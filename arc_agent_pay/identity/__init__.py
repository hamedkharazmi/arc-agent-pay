"""
arc_agent_pay.identity — ERC-8004 agent identity, reputation + validation (optional).

    from arc_agent_pay.identity import AgentIdentity, ReputationClient, ValidationClient

Requires the `[onchain]` extra (web3). Identity is entirely optional — the
payment SDK works with no identity configured.
"""

from .erc8004 import AgentIdentity, ReputationClient, ValidationClient

__all__ = ["AgentIdentity", "ReputationClient", "ValidationClient"]
