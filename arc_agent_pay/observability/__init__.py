"""
arc_agent_pay.observability — tracing + evaluation for agent runs.

`tracing.get_callbacks()` returns LangChain callbacks for the graph when Langfuse
is configured (env keys), and an empty list otherwise — so observability is
zero-config and never a hard dependency.

The `evals` subpackage provides an offline evaluation harness for the agent's
service-discovery quality and budget adherence.
"""

from .tracing import get_callbacks, is_tracing_enabled

__all__ = ["get_callbacks", "is_tracing_enabled"]
