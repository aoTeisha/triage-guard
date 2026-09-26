"""Prolog over `rules/monitor.pl` (SWI-Prolog via pyswip): who may act, and
what should happen to one claimed timer, with an explanation for every deny.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyswip import Prolog

RULES = Path(__file__).parent / "rules" / "monitor.pl"
SAFETY_RULES = Path(__file__).parent / "rules" / "safety.pl"

# ponytail: one process-wide engine behind one lock. pyswip's engine is a
# global and is not thread-safe; the board's FastAPI threadpool reaches
# `actor_is_charge` from several threads. Per-thread engines if this lock
# ever shows up in a profile.
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _engine() -> "Prolog":
    # Imported here, not at module scope: pyswip's `_find_swipl()` runs at
    # import time and raises immediately if SWI-Prolog isn't installed. A
    # module-scope import would then fail the whole app's import (every
    # graph node imports `app.deterministic`, which imports this module),
    # not just Prolog-gated actions. Importing lazily lets that failure
    # surface here instead, where `_holds`/`timer_action` already convert any
    # engine fault (this one included) into the documented fail-closed deny.
    from pyswip import Prolog

    engine = Prolog()
    engine.consult(str(RULES))
    engine.consult(str(SAFETY_RULES))
    return engine


def atom(value: Any) -> str:
    """Quote a Python value as a Prolog atom. Roles and ids arrive from HTTP
    input, so they are never interpolated raw into a goal — a stray quote
    would otherwise be parsed as Prolog.
    """
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


class PrologEngineError(RuntimeError):
    """Raised by `_holds` when the engine itself faults during a query — as
    opposed to the query simply failing to hold. Kept distinct so callers can
    tell "no" from "the engine couldn't answer" and refuse with a reason
    instead of letting the fault escape as an unhandled exception (fail
    closed, same principle `timer_action` already follows for the sweeper).
    """


def _holds(goal: str) -> bool:
    with _lock:
        try:
            return bool(list(_engine().query(goal)))
        except Exception as exc:  # noqa: BLE001 — any engine fault must reach the caller as a typed refusal, never crash it
            raise PrologEngineError(str(exc)) from exc


def charge_role(role: str) -> tuple[bool, str]:
    """A charge role resolves the gate and signs a release."""
    try:
        holds = _holds(f"charge_role({atom(role)})")
    except PrologEngineError as exc:
        return False, f"gate refused: prolog engine unavailable: {exc}"
    if holds:
        return True, f"{role} holds charge role"
    return False, f"gate refused: role {role!r} is not a charge role"


def charge_roles() -> frozenset[str]:
    """Every role `charge_role/1` holds, read from the engine rather than copied.

    `app.symbolic.datalog.acuity_provenance` needs the same set to decide whether
    an acuity's writer was authorized; asking Prolog keeps one source. An engine
    fault returns the empty set, so every writer reads as unauthorized and the
    verdict fails closed.
    """
    with _lock:
        try:
            return frozenset(str(row["R"]) for row in _engine().query("charge_role(R)"))
        except Exception:  # noqa: BLE001 — an engine fault must fail closed, not crash the verdict
            return frozenset()


def may_resolve_gate(role: str, *, senior_required: bool) -> tuple[bool, str]:
    """Who may answer this gate: a charge role normally, only a shift lead
    once the correction loop handed the case up.
    """
    try:
        holds = _holds(f"may_resolve_gate({atom(role)}, {str(senior_required).lower()})")
    except PrologEngineError as exc:
        return False, f"gate refused: prolog engine unavailable: {exc}"
    if holds:
        return True, (f"{role} may decide" if not senior_required else "a shift lead must decide")
    if senior_required:
        return False, f"gate refused: role {role!r}; a shift lead must decide"
    return False, f"gate refused: role {role!r} is not a charge role"


def timer_action(ctx: dict[str, Any]) -> tuple[str, str]:
    """`(action, why)` for one claimed timer, from the same facts the BPpy
    b-threads select on — so `fire.handle` can insist the two layers agree.
    `why` lists every `denial/3` that holds, joined by `; `, or is empty.
    An engine failure returns `("engine_unavailable", <error>)` so the
    caller refuses rather than guesses.
    """
    tid = atom(ctx["timer_id"])
    facts = [
        f"timer({tid}, {atom(ctx['kind'])}, {atom(ctx['fire_state'])})",
        f"notify_count({tid}, {int(ctx['notify_count'])})",
        f"notify_budget({tid}, {int(ctx['notify_budget'])})",
    ]
    if ctx["pause_active"]:
        facts.append(f"pause_active({tid})")
    with _lock:
        engine = None
        try:
            engine = _engine()
            for fact in facts:
                engine.assertz(fact)
            actions = [s["A"] for s in engine.query(f"action({tid}, A)")]
            denials = [f"{s['E']}: {s['W']}" for s in engine.query(f"denial(E, {tid}, W)")]
        except Exception as exc:  # noqa: BLE001 — any engine fault is a refusal, never a guess
            return "engine_unavailable", f"prolog: {exc}"
        finally:
            if engine is not None:
                for pred in (f"timer({tid}, _, _)", f"notify_count({tid}, _)",
                             f"notify_budget({tid}, _)", f"pause_active({tid})"):
                    engine.retractall(pred)
    return (actions[0] if actions else "cancel"), "; ".join(denials)


# The facts safety.pl reasons over. Retracted after every query, so one case's
# facts can never leak into the next.
_SAFETY_FACTS = ("acuity(_)", "acuity_source(_)", "gap(_)", "nurse_proposal(_)",
                 "system_proposal(_)", "human_decided", "classifier_down")


def _term(value: Any) -> str:
    """A Python value as a Prolog term: numbers bare, everything else quoted."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else atom(value)


def safety_violations(case: dict[str, Any]) -> tuple[list[str], str]:
    """`(violation codes, engine error)` for one case, from `rules/safety.pl`.

    An engine fault returns no codes and a non-empty error: the caller must
    treat that as a refusal, not as a pass (SYSTEM_MODELING.md:101 — a validator
    that cannot answer routes the case to a human, it does not wave it through).
    """
    facts = [
        f"acuity({_term(case.get('acuity') or 'none')})",
        f"acuity_source({_term(case.get('acuity_source') or 'none')})",
        f"gap({_term(case['gap'] if case.get('gap') is not None else 'unknown')})",
        f"nurse_proposal({_term(case.get('nurse_proposal') or 'none')})",
        f"system_proposal({_term(case.get('system_proposal') or 'none')})",
    ]
    if case.get("human_decided"):
        facts.append("human_decided")
    if case.get("classifier_down"):
        facts.append("classifier_down")

    with _lock:
        engine = None
        try:
            engine = _engine()
            for fact in facts:
                engine.assertz(fact)
            codes = sorted({str(row["C"]) for row in engine.query("violation(C)")})
        except Exception as exc:  # noqa: BLE001 — an engine fault is a refusal, never a pass
            return [], f"prolog: {exc}"
        finally:
            if engine is not None:
                for pred in _SAFETY_FACTS:
                    engine.retractall(pred)
    return codes, ""
