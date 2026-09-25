"""Langfuse wiring.

Three entry points:

  langfuse_callbacks()  the LangGraph callback handler. Passed in the invoke config,
                        it traces every node automatically — no per-step
                        instrumentation to write or forget.
  case_trace()          the root span around every graph invoke on a case:
                        session = case, user = hashed patient, tags, metadata.
  record_outcome()      the invoke's decisions as span output, plus trace scores.

Every value the SDK records passes through `_mask`, so no patient identifier
reaches Langfuse.

Client construction is lazy and cached. It used to run at import time, which made
importing this module a network-adjacent side effect and forced every caller to
worry about `.env` load order.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv


def tracing_enabled() -> bool:
    """Off unless Langfuse is configured. Keeps the offline test suite silent and
    keyless without every caller guarding its own calls.
    """
    load_dotenv()
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def patient_ref(stable_patient_id: str | None) -> str | None:
    """A patient's Langfuse `user_id`: a keyed hash, never the ID itself.

    The ID is nine digits, so a plain hash is reversed by trying all 10^9 of
    them. Keying it with `TRIAGE_TRACE_SALT` closes that; with no salt there
    is no `user_id` at all rather than a weak one. Staff find a patient's
    traces with `uv run python -m app.observability <id>`.
    """
    salt = os.environ.get("TRIAGE_TRACE_SALT", "")
    if not stable_patient_id or not salt:
        return None
    digest = hmac.new(salt.encode(), stable_patient_id.encode(), hashlib.sha256).hexdigest()
    return f"pt-{digest[:16]}"


def _fit(value: Any) -> str:
    """Langfuse drops attribute values that are not ASCII or exceed 200 chars."""
    return str(value).encode("ascii", "replace").decode()[:200]


def trace_attributes(case_id: str, operation: str, values: dict[str, Any]) -> dict[str, Any]:
    """Trace-level fields for one graph invoke on one case.

    `session_id = case_id` groups the start, every resume, every timer fire
    and every board action of a case into one Langfuse session. `values` is
    whatever is known about the case before the invoke: the intake payload
    for a new case, the checkpointed state otherwise.
    """
    llm_mode = os.environ.get("TRIAGE_LLM", "mock").strip().lower()
    tags = [operation, f"llm-{llm_mode}"]
    if values.get("channel"):
        tags.append(f"channel-{values['channel']}")
    if values.get("submission_type"):
        tags.append(str(values["submission_type"]))
    metadata = {"case_id": case_id, "operation": operation, "llm_mode": llm_mode}
    if llm_mode == "live" and os.environ.get("MODEL"):
        metadata["model"] = os.environ["MODEL"]
    return {
        "session_id": _fit(case_id),
        "user_id": patient_ref(values.get("stable_patient_id")),
        "trace_name": _fit(operation),
        "tags": [_fit(t) for t in tags],
        "metadata": {k: _fit(v) for k, v in metadata.items()},
    }


def outcome_scores(before: dict[str, Any], after: Any) -> list[tuple[str, float | str, str]]:
    """Trace scores for one invoke: `(name, value, data_type)`.

    These are the dashboard's dimensions. `guardrail_blocks` counts only the
    refusals this invoke added, so summing it over a case's traces does not
    count one refusal once per later resume.
    """
    if not isinstance(after, dict):
        return []
    from app.labels import Transition
    from app.verification import check_trace

    state = after.get("control_state")
    scores: list[tuple[str, float | str, str]] = [
        ("control_state", str(getattr(state, "value", state) or "unknown"), "CATEGORICAL")]
    for name in ("acuity", "acuity_gap"):
        if after.get(name) is not None:
            scores.append((name, float(after[name]), "NUMERIC"))
    log = after.get("audit_log") or []
    new = log[len(before.get("audit_log") or []):]
    blocks = sum(1 for r in new if r.get("transition") == Transition.BLK.value)
    scores.append(("guardrail_blocks", float(blocks), "NUMERIC"))
    if log:
        scores.append(("trace_check", 1.0 if check_trace(log).passed else 0.0, "BOOLEAN"))
    return scores


def _mask(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: no national ID, phone or email leaves the process.

    Runs on every input, output and metadata value the SDK records, which
    includes the LangGraph node spans (their inputs carry `raw_payload`).
    The redactor only walks strings, dicts and lists, so anything else — a
    Pydantic state, a `Command(resume=...)`, a model nested in a dict — is
    first turned into plain JSON with the SDK's own serializer, the same
    shape Langfuse would export.
    """
    from langfuse._utils.serializer import EventSerializer

    from app.guards.identifiers import redact_identifiers

    if not isinstance(data, (str, int, float, bool, type(None))):
        data = json.loads(json.dumps(data, cls=EventSerializer))
    return redact_identifiers(data)


@lru_cache(maxsize=1)
def client():
    """Built once, with the identifier mask. The LangGraph callback handler
    looks the client up by public key, so building this one first is what
    puts the mask on the node spans too.
    """
    load_dotenv()
    from langfuse import Langfuse

    return Langfuse(mask=_mask)


@lru_cache(maxsize=1)
def _handler():
    from langfuse.langchain import CallbackHandler

    client()
    return CallbackHandler()


def langfuse_callbacks() -> list[Any]:
    """Callback list for a LangGraph invoke config. Empty when tracing is off."""
    if not tracing_enabled():
        return []
    try:
        return [_handler()]
    except Exception as exc:
        # Observability must never take the pipeline down with it — but a silent
        # [] here looks identical to tracing being off, and hid a missing
        # `langchain` install for a whole afternoon. Warn once, then degrade.
        logging.getLogger(__name__).warning("Langfuse tracing disabled: %s", exc)
        return []


@contextmanager
def case_trace(case_id: str, operation: str, values: dict[str, Any]):
    """Root span for one graph invoke on one case. A no-op when tracing is off.

    Everything the LangGraph handler records inside it inherits the session,
    patient, tags and metadata, so the trace table shows `case-start`,
    `case-resume`, `timer-fire` or `board-action` instead of `LangGraph`.
    """
    if not tracing_enabled():
        yield _NullSpan()
        return
    stack = ExitStack()
    try:
        from langfuse import propagate_attributes

        attrs = trace_attributes(case_id, operation, values)
        span = stack.enter_context(
            client().start_as_current_observation(name=operation, as_type="span"))
        stack.enter_context(propagate_attributes(**attrs))
    except Exception as exc:
        # Whatever did open (the span) is closed, so no dead span is left as
        # the parent of this thread's later work.
        _close(stack, (None, None, None))
        logging.getLogger(__name__).warning("Langfuse case trace disabled: %s", exc)
        yield _NullSpan()
        return
    try:
        yield span
    except BaseException:
        _close(stack, sys.exc_info())   # the span is marked failed; the error propagates
        raise
    _close(stack, (None, None, None))


def _close(stack: ExitStack, exc_info: tuple) -> None:
    """Exit the trace contexts without ever replacing the case's own error."""
    try:
        stack.__exit__(*exc_info)
    except Exception as exc:
        logging.getLogger(__name__).warning("Langfuse case trace not closed cleanly: %s", exc)


def record_outcome(span: Any, before: dict[str, Any], after: Any) -> None:
    """Span output = what this invoke decided; trace scores for the dashboard.

    The output lists only the audit records this invoke added: each carries
    the decision fields (action, explanation, transition, denying layer), so
    a trace answers "what was decided and why" without opening the node spans.
    """
    if isinstance(span, _NullSpan) or not isinstance(after, dict):
        return
    try:
        log = after.get("audit_log") or []
        state = after.get("control_state")
        span.update(output={
            "control_state": getattr(state, "value", state),
            "paused": bool(after.get("__interrupt__")),
            "decisions": log[len(before.get("audit_log") or []):],
        })
        for name, value, data_type in outcome_scores(before, after):
            span.score_trace(name=name, value=value, data_type=data_type)
    except Exception as exc:
        logging.getLogger(__name__).warning("Langfuse outcome not recorded: %s", exc)


class _NullSpan:
    """Stand-in so callers can always call `.update(...)` and `.score_trace(...)`."""

    def update(self, **_: Any) -> None:
        return None

    def score_trace(self, **_: Any) -> None:
        return None


def flush() -> None:
    if tracing_enabled():
        try:
            client().flush()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> None:
    """`uv run python -m app.observability <stable_patient_id>` prints the
    patient's Langfuse user id, to paste into the Users page or a filter.
    """
    import sys

    load_dotenv()
    for patient_id in (argv if argv is not None else sys.argv[1:]):
        print(patient_ref(patient_id) or "TRIAGE_TRACE_SALT is not set")


if __name__ == "__main__":
    main()
