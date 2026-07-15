"""
observability/tracing.py — optional LangGraph/LangChain tracing via Langfuse.

Featured integration: Langfuse (free self-host via Docker, or free cloud tier).
It is entirely opt-in and self-hostable, so the public repo has no paid hard
dependency. When the LANGFUSE_* env vars are unset, `get_callbacks()` returns an
empty list and the agent runs with no tracing overhead.

Enable by installing the extra and setting env vars:
    pip install "arc-agent-pay[observability]"
    export LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... LANGFUSE_HOST=...

For vendor-neutral tracing, point the LangChain callbacks at an OpenTelemetry
collector instead — the wiring below is the only place that would change.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def is_tracing_enabled() -> bool:
    """True if Langfuse credentials are present in the environment."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def get_callbacks() -> list:
    """
    Return LangChain callback handlers for tracing, or [] if not configured.

    Pass the result into the graph run, e.g.:
        graph.ainvoke(state, {"callbacks": get_callbacks()})
    """
    if not is_tracing_enabled():
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        logger.warning(
            "LANGFUSE_* set but langfuse is not installed; "
            'install with: pip install "arc-agent-pay[observability]"'
        )
        return []

    try:
        return [CallbackHandler()]
    except Exception as e:  # noqa: BLE001 - never let tracing break a run
        logger.warning("Failed to initialize Langfuse tracing: %s", e)
        return []
