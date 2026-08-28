"""Langfuse wiring. One span per agent, nested under a per-case root span."""

from contextlib import contextmanager

from langfuse import get_client

langfuse = get_client()


@contextmanager
def agent_span(name: str, **metadata):
    """Open a Langfuse span named for an agent or a case.

    Caller updates it with .update(input=..., output=...).
    """
    with langfuse.start_as_current_observation(name=name, as_type="span") as span:
        span.update(metadata={"mock": True, **metadata})
        yield span
