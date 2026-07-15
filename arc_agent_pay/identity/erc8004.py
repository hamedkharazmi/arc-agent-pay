"""
identity/erc8004.py — ERC-8004 agent identity + reputation on Arc Testnet.

Gives an agent a verifiable onchain identity (an ERC-721 "agent id") and lets you
read its reputation. Read operations need no gas; write operations (register,
give_feedback) require a funded account and are opt-in.

Requires the `[onchain]` extra:  pip install "arc-agent-pay[onchain]"

Contract addresses + ABIs live in arc_agent_pay/onchain (verified from the Arc
docs). For testing, inject `w3` / `contract` so no chain access is needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..models import AgentProfile
from ..onchain import (
    identity_registry_address,
    load_abi,
    reputation_registry_address,
    rpc_url,
    validation_registry_address,
)

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _connect(rpc: Optional[str] = None):
    """Build a Web3 client (lazy import so core install stays light)."""
    try:
        from web3 import Web3
    except ImportError as e:  # pragma: no cover - import guard
        raise ImportError(
            'ERC-8004 identity requires web3.\n'
            'Install with: pip install "arc-agent-pay[onchain]"'
        ) from e
    return Web3(Web3.HTTPProvider(rpc or rpc_url()))


def _wait_success(w3: Any, tx_hash: Any) -> Any:
    """Wait for a tx and raise if it reverted on-chain (status == 0).

    web3's wait_for_transaction_receipt returns even for reverted txs, so callers
    that don't check `status` would treat an on-chain revert as success. Writes
    here are best-effort at the call site, so surfacing the revert lets them fail
    cleanly instead of reporting a bogus tx hash.
    """
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if int(receipt.get("status", 1)) == 0:
        raise RuntimeError(f"transaction reverted on-chain (tx {tx_hash.hex()})")
    return receipt


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class AgentIdentity:
    """Read/write an agent's ERC-8004 identity (the Identity Registry, ERC-721)."""

    def __init__(
        self,
        *,
        account: Any = None,            # eth_account LocalAccount, required for writes
        rpc: Optional[str] = None,
        w3: Any = None,                 # inject for tests
        contract: Any = None,           # inject for tests
    ) -> None:
        self.account = account
        self.w3 = w3 or (_connect(rpc) if contract is None else None)
        if contract is not None:
            self.contract = contract
        else:
            self.contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(identity_registry_address()),
                abi=load_abi("identity_registry"),
            )

    # --- reads (no gas) ---

    def owner_of(self, agent_id: int) -> str:
        return self.contract.functions.ownerOf(agent_id).call()

    def token_uri(self, agent_id: int) -> str:
        return self.contract.functions.tokenURI(agent_id).call()

    def resolve(
        self,
        address: str,
        *,
        max_lookback_blocks: int = 200_000,
        chunk_blocks: int = 9_000,
    ) -> Optional[int]:
        """
        Best-effort: find the most recent agent id minted to `address` by scanning
        Transfer mint logs (from == 0x0) newest-first in bounded chunks.

        Public RPCs reject an unbounded eth_getLogs (HTTP 413), so the scan is
        capped to the last `max_lookback_blocks` blocks and split into
        `chunk_blocks`-sized windows. Returns None if not found in that window or
        on any error.
        """
        try:
            latest = self.w3.eth.block_number
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read chain head for resolve(%s): %s", address, e)
            return None

        floor = max(0, latest - max_lookback_blocks)
        to_block = latest
        while to_block >= floor:
            from_block = max(floor, to_block - chunk_blocks + 1)
            try:
                logs = self.contract.events.Transfer().get_logs(
                    from_block=from_block,
                    to_block=to_block,
                    argument_filters={"from": ZERO_ADDRESS, "to": address},
                )
                if logs:
                    return int(logs[-1]["args"]["tokenId"])
            except Exception as e:  # noqa: BLE001 - skip a bad window, keep scanning
                logger.debug("resolve chunk %s-%s failed: %s", from_block, to_block, e)
            to_block = from_block - 1
        return None

    # --- writes (gas; opt-in) ---

    def register(self, metadata_uri: str = "") -> int:
        """
        Register this account's agent identity. Returns the new agent id.
        Requires `account` (a funded EOA) — costs gas on Arc Testnet.
        """
        if self.account is None:
            raise ValueError("register() requires an account (a funded EOA).")

        fn = (
            self.contract.functions.register(metadata_uri)
            if metadata_uri
            else self.contract.functions.register()
        )
        tx = fn.build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        # The agent id is the tokenId from the mint Transfer event.
        events = self.contract.events.Transfer().process_receipt(receipt)
        for ev in events:
            if ev["args"].get("from") == ZERO_ADDRESS:
                return int(ev["args"]["tokenId"])
        raise RuntimeError("register() succeeded but no mint event was found.")

    def profile(self, agent_id: int, reputation: Optional["ReputationClient"] = None) -> AgentProfile:
        """Build an AgentProfile combining identity + (optional) reputation."""
        profile = AgentProfile(
            agent_id=agent_id,
            address=self.owner_of(agent_id),
            metadata_uri=self._safe_token_uri(agent_id),
        )
        if reputation is not None:
            count, value = reputation.summary(agent_id)
            profile.feedback_count = count
            profile.reputation_score = value
        return profile

    def _safe_token_uri(self, agent_id: int) -> str:
        try:
            return self.token_uri(agent_id)
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------
# Reputation
# ---------------------------------------------------------------------------

class ReputationClient:
    """Read/write an agent's ERC-8004 reputation (the Reputation Registry)."""

    def __init__(
        self,
        *,
        account: Any = None,
        rpc: Optional[str] = None,
        w3: Any = None,
        contract: Any = None,
    ) -> None:
        self.account = account
        self.w3 = w3 or (_connect(rpc) if contract is None else None)
        if contract is not None:
            self.contract = contract
        else:
            self.contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(reputation_registry_address()),
                abi=load_abi("reputation_registry"),
            )

    def summary(self, agent_id: int) -> tuple[int, float]:
        """
        Return (feedback_count, aggregate_score) for an agent.

        Arc's getSummary requires the client addresses to summarize over (an
        empty list reverts with "clientAddresses required"), so we first read
        getClients(agentId). No clients => no feedback => (0, 0.0). Otherwise
        getSummary returns (count, value, decimals) and we scale value.
        """
        clients = self.contract.functions.getClients(agent_id).call()
        if not clients:
            return 0, 0.0
        count, value, decimals = self.contract.functions.getSummary(
            agent_id, clients, "", ""
        ).call()
        score = value / (10 ** decimals) if decimals else float(value)
        return int(count), score

    def give_feedback(
        self,
        agent_id: int,
        score: int,
        *,
        value_decimals: int = 0,
        tag1: str = "",
        tag2: str = "",
        endpoint: str = "",
        feedback_uri: str = "",
        feedback_hash: bytes = b"\x00" * 32,
    ) -> str:
        """Submit feedback for an agent (write; needs a funded account). Returns tx hash."""
        if self.account is None:
            raise ValueError("give_feedback() requires an account (a funded EOA).")

        tx = self.contract.functions.giveFeedback(
            agent_id, score, value_decimals, tag1, tag2, endpoint, feedback_uri, feedback_hash
        ).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        _wait_success(self.w3, tx_hash)
        return tx_hash.hex()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _to_bytes32(value: Any) -> bytes:
    """Normalize a request/response hash to 32 raw bytes (accepts bytes or hex)."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    else:
        raise TypeError("hash must be bytes or a hex string.")
    if len(raw) != 32:
        raise ValueError("hash must be exactly 32 bytes.")
    return raw


class ValidationClient:
    """Read/write an agent's ERC-8004 validations (the Validation Registry).

    Validation is the third ERC-8004 registry: a *validator* attests that an
    agent's work was checked. The flow is two-sided —

      1. `request_validation(...)` records that `validator_address` should validate
         a piece of the agent's work, keyed by `request_hash`.
      2. the validator calls `respond(...)` with a 0-100 score (0 = failed,
         100 = passed) plus optional off-chain evidence (URIs/hashes/tag).

    Reads need no gas; writes need a funded account and are opt-in.
    """

    def __init__(
        self,
        *,
        account: Any = None,
        rpc: Optional[str] = None,
        w3: Any = None,
        contract: Any = None,
    ) -> None:
        self.account = account
        self.w3 = w3 or (_connect(rpc) if contract is None else None)
        if contract is not None:
            self.contract = contract
        else:
            self.contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(validation_registry_address()),
                abi=load_abi("validation_registry"),
            )

    # --- reads (no gas) ---

    def _get_status(self, request_hash: Any):
        return self.contract.functions.getValidationStatus(_to_bytes32(request_hash)).call()

    @staticmethod
    def _exists(validator: Any) -> bool:
        return bool(validator) and str(validator).lower() != ZERO_ADDRESS.lower()

    def request_exists(self, request_hash: Any) -> bool:
        # This deployment has no requestExists(); a set validator address on the
        # validation record is what tells us a request has been made.
        return self._exists(self._get_status(request_hash)[0])

    def status(self, request_hash: Any) -> Optional[dict]:
        """Return the validation record for a request, or None if it doesn't exist."""
        validator, agent_id, response, response_hash, tag, last_update = self._get_status(
            request_hash
        )
        if not self._exists(validator):
            return None
        rh_hex = "0x" + response_hash.hex() if isinstance(response_hash, bytes) else response_hash
        return {
            "validator_address": validator,
            "agent_id": int(agent_id),
            "response": int(response),
            "response_hash": rh_hex,
            "tag": tag,
            "last_update": int(last_update),
            # response is 0-100 (0 can also mean "failed"); >0 means a score landed.
            "responded": int(response) > 0,
        }

    def summary(self, agent_id: int, validators: list[str], tag: str = "") -> tuple[int, int]:
        """Return (count, average_response 0-100) over the given validator addresses.

        Like the Reputation Registry, getSummary needs the validator addresses to
        aggregate over; an empty list means no validations to summarize -> (0, 0).
        """
        if not validators:
            return 0, 0
        count, avg = self.contract.functions.getSummary(agent_id, validators, tag).call()
        return int(count), int(avg)

    def agent_validations(self, agent_id: int) -> list[str]:
        """Return the request hashes (hex) recorded for an agent."""
        hashes = self.contract.functions.getAgentValidations(agent_id).call()
        return ["0x" + h.hex() if isinstance(h, bytes) else h for h in hashes]

    # --- writes (gas; opt-in) ---

    def request_validation(
        self,
        *,
        validator_address: str,
        agent_id: int,
        request_hash: Any,
        request_uri: str = "",
    ) -> str:
        """Record a validation request for an agent's work. Returns tx hash."""
        if self.account is None:
            raise ValueError("request_validation() requires an account (a funded EOA).")
        tx = self.contract.functions.validationRequest(
            self.w3.to_checksum_address(validator_address),
            agent_id,
            request_uri,
            _to_bytes32(request_hash),
        ).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        _wait_success(self.w3, tx_hash)
        return tx_hash.hex()

    def respond(
        self,
        *,
        request_hash: Any,
        response: int,
        response_uri: str = "",
        response_hash: bytes = b"\x00" * 32,
        tag: str = "",
        confirm_ready: bool = False,
        ready_attempts: int = 10,
        ready_delay: float = 3.0,
    ) -> str:
        """Submit a validator's 0-100 response to a request. Returns tx hash.

        A response fired immediately after its request can revert: on a
        load-balanced RPC the request may not be visible yet on the node that
        executes the response. With `confirm_ready=True` we poll a dry-run
        (eth_call) until it would succeed before spending gas, so we don't burn a
        reverting tx while the request propagates.
        """
        if self.account is None:
            raise ValueError("respond() requires an account (a funded EOA).")
        if not 0 <= response <= 100:
            raise ValueError("response must be between 0 and 100.")
        fn = self.contract.functions.validationResponse(
            _to_bytes32(request_hash),
            response,
            response_uri,
            _to_bytes32(response_hash),
            tag,
        )
        if confirm_ready:
            last_err: Optional[Exception] = None
            for attempt in range(ready_attempts):
                try:
                    fn.call({"from": self.account.address})
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001 - request not visible yet; retry
                    last_err = e
                    if attempt < ready_attempts - 1:
                        time.sleep(ready_delay)
            if last_err is not None:
                raise last_err
        tx = fn.build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        _wait_success(self.w3, tx_hash)
        return tx_hash.hex()
