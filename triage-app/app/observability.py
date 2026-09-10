"""Langfuse wiring.

Two entry points, because there are two callers with different needs:

  langfuse_callbacks()  the LangGraph callback handler. Passed in the invoke config,
                        it traces every node automatically — no per-step
                        instrumentation to write or forget.
  agent_span()          a manual span, used by intake-channel for the work it does
                        outside the graph (CRM lookup, payload construction).

Client construction is lazy and cached. It used to run at import time, which made
importing this module a network-adjacent side effect and forced every caller to
worry about `.env` load order.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv


def tracing_enabled() -> bool:
    """Off unless Langfuse is configured. Keeps the offline test suite silent and
    keyless without every caller guarding its own calls.
    """
    load_dotenv()
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


@lru_cache(maxsize=1)
def client():
    load_dotenv()
    from langfuse import get_client

    return get_client()


@lru_cache(maxsize=1)
def _handler():
    from langfuse.langchain import CallbackHandler

    return CallbackHandler()


def langfuse_callbacks() -> list[Any]:
    """Callback list for a LangGraph invoke config. Empty when tracing is off."""
    if not tracing_enabled():
        return []
    try:
        return [_handler()]
    except Exception:
        # Observability must never take the pipeline down with it.
        return []


@contextmanager
def agent_span(name: str, **metadata: Any):
    """Manual span for work outside the graph. A no-op when tracing is off."""
    if not tracing_enabled():
        yield _NullSpan()
        return
    with client().start_as_current_observation(name=name, as_type="span") as span:
        span.update(metadata=metadata)
        yield span


class _NullSpan:
    """Stand-in so callers can always call `.update(...)`."""

    def update(self, **_: Any) -> None:
        return None


def flush() -> None:
    if tracing_enabled():
        try:
            client().flush()
        except Exception:
            pass
