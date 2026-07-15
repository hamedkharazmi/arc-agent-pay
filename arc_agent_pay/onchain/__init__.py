"""
arc_agent_pay.onchain — packaged contract addresses + ABIs.

Single source of truth for the ERC-8004 registry addresses (verified from the
Arc docs) and their minimal ABIs. Addresses can be overridden per-deployment via
environment variables so the SDK keeps working if Arc redeploys:

    ERC8004_IDENTITY_REGISTRY, ERC8004_REPUTATION_REGISTRY,
    ERC8004_VALIDATION_REGISTRY, ARC_TESTNET_RPC
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent
_ABI_DIR = _DIR / "abi"


@lru_cache(maxsize=1)
def _addresses() -> dict:
    return json.loads((_DIR / "addresses.json").read_text())["arc_testnet"]


@lru_cache(maxsize=None)
def load_abi(name: str) -> list:
    """Load a packaged ABI by file stem, e.g. load_abi('identity_registry')."""
    return json.loads((_ABI_DIR / f"{name}.json").read_text())


def rpc_url() -> str:
    return os.environ.get("ARC_TESTNET_RPC") or _addresses()["rpc_url"]


def chain_id() -> int:
    return _addresses()["chain_id"]


def identity_registry_address() -> str:
    return os.environ.get("ERC8004_IDENTITY_REGISTRY") or _addresses()["identity_registry"]


def reputation_registry_address() -> str:
    return os.environ.get("ERC8004_REPUTATION_REGISTRY") or _addresses()["reputation_registry"]


def validation_registry_address() -> str:
    return os.environ.get("ERC8004_VALIDATION_REGISTRY") or _addresses()["validation_registry"]
