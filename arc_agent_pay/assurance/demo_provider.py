"""Minimal real x402 v2 provider for the AgentPay Assurance testnet demo.

The HTTP wrapper is intentionally tiny. Wire models, base64 codecs, EIP-3009
verification, calldata, and transfer-event checks come from the installed x402
SDK so the demo speaks exactly the same dialect as :class:`PaymentClient`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from web3 import Web3
from x402.http.utils import (
    decode_payment_signature_header,
    encode_payment_required_header,
    encode_payment_response_header,
)
from x402.mechanisms.evm.exact.eip3009_utils import (
    execute_transfer_with_authorization,
    parse_eip3009_authorization,
    verify_eip3009_transfer_event,
)
from x402.mechanisms.evm.exact.facilitator import ExactEvmScheme
from x402.mechanisms.evm.types import ExactEIP3009Payload
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
)

from ..models import AssuranceTerms, Chain, Service
from ..onchain import rpc_url
from .genlayer_transport import GenLayerRetryPolicy, is_transient_transport_error


ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_NETWORK = "eip155:5042002"
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
WARRANTY_BOND = "0x74c4b5006136d37134B60F4Ff952E446BC11C901"
PROVIDER = "0x5cBC83e3363710772B8812Ad3AB5423116011651"
ASSURANCE_OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
PAYMENT_AMOUNT = 1_000_000
MAX_TIMEOUT_SECONDS = 300
GAS_LIMIT_MULTIPLIER = 1.20

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"

BAD_RESPONSE = {"brief": "DOGE will definitely outperform everything this week."}
BAD_RESPONSE_BYTES = json.dumps(BAD_RESPONSE, sort_keys=True, separators=(",", ":")).encode()

DEMO_ACCEPTANCE_CRITERIA = [
    "Cover BTC, ETH, and SOL.",
    "Clearly distinguish factual information from analysis.",
    "Include at least one material risk or caveat.",
]

_HEX_32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_SIGNATURE = re.compile(r"^0x[0-9a-fA-F]{130}$")


class DemoProviderError(RuntimeError):
    """Base error for the narrowly scoped live demo provider."""


class DemoPaymentRejected(DemoProviderError):
    """The supplied x402 payment is malformed or does not match the quote."""


class DemoSettlementOutcomeUnknown(DemoProviderError):
    """A broadcast may have succeeded, so the authorization must not be resent."""


class DemoSettlementPending(DemoProviderError):
    """A known transaction must be resolved rather than submitted again."""

    def __init__(self, transaction_hash: str):
        self.transaction_hash = transaction_hash
        super().__init__(f"settlement remains pending: {transaction_hash}")


class DemoSettlementFailed(DemoProviderError):
    """The known Arc settlement transaction failed or lacked the exact transfer."""


@dataclass(frozen=True)
class DemoHTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class DemoSettler(Protocol):
    def verify(self, payload: PaymentPayload) -> str: ...

    def submit(self, payload: PaymentPayload) -> str: ...

    def wait(self, transaction_hash: str) -> Mapping[str, Any]: ...

    def transfer_matches(self, receipt: Mapping[str, Any], payload: PaymentPayload) -> bool: ...


def demo_service(url: str) -> Service:
    """Return the frozen service/SLA snapshot used by the real demo payment."""
    return Service(
        name="AI Market Brief Assurance Demo",
        url=url,
        description=(
            "Produces a concise BTC, ETH, and SOL market brief from supplied information."
        ),
        price_usdc="1",
        chain=Chain.ARC_TESTNET,
        tags=["assurance", "market-brief", "demo"],
        assurance=AssuranceTerms(
            version="1",
            provider_address=PROVIDER,
            warranty_bond_address=WARRANTY_BOND,
            acceptance_criteria=DEMO_ACCEPTANCE_CRITERIA,
            max_response_time_ms=2_000,
            dispute_window_seconds=86_400,
            refund_bps=10_000,
        ),
    )


def payment_requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network=ARC_TESTNET_NETWORK,
        asset=ARC_TESTNET_USDC,
        amount=str(PAYMENT_AMOUNT),
        payTo=PROVIDER,
        maxTimeoutSeconds=MAX_TIMEOUT_SECONDS,
        extra={"name": "USDC", "version": "2"},
    )


def payment_required(url: str, *, error: str = "Payment required") -> PaymentRequired:
    return PaymentRequired(
        x402Version=2,
        error=error,
        resource=ResourceInfo(
            url=url,
            description="AgentPay Assurance intentionally bad market brief",
            mimeType="application/json",
            serviceName="AI Market Brief Assurance Demo",
        ),
        accepts=[payment_requirements()],
    )


def _authorization_commitment(payload: PaymentPayload) -> str:
    """Commit to the one-time authorization identity, not mutable outer fields."""
    authorization = _authorization(payload).authorization
    canonical = json.dumps(
        {
            "asset": ARC_TESTNET_USDC.lower(),
            "from": authorization.from_address.lower(),
            "network": ARC_TESTNET_NETWORK,
            "nonce": authorization.nonce.lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "0x" + hashlib.sha256(canonical.encode()).hexdigest()


def _authorization(payload: PaymentPayload) -> ExactEIP3009Payload:
    try:
        return ExactEIP3009Payload.from_dict(payload.payload)
    except Exception as exc:
        raise DemoPaymentRejected("malformed EIP-3009 payload") from exc


def validate_payment_payload(
    payload: PaymentPayload,
    *,
    now: int | None = None,
) -> ExactEIP3009Payload:
    """Validate every deterministic field before signature simulation/broadcast."""
    if payload.x402_version != 2:
        raise DemoPaymentRejected("x402 version must be 2")
    accepted = payload.accepted
    if accepted.scheme != "exact":
        raise DemoPaymentRejected("payment scheme must be exact")
    if str(accepted.network) != ARC_TESTNET_NETWORK:
        raise DemoPaymentRejected("payment network does not match the advertised quote")
    try:
        asset = Web3.to_checksum_address(accepted.asset)
        pay_to = Web3.to_checksum_address(accepted.pay_to)
    except Exception as exc:
        raise DemoPaymentRejected("payment quote contains an invalid address") from exc
    if asset != Web3.to_checksum_address(ARC_TESTNET_USDC):
        raise DemoPaymentRejected("payment asset does not match the advertised quote")
    if pay_to != Web3.to_checksum_address(PROVIDER):
        raise DemoPaymentRejected("payment recipient does not match the advertised quote")
    if accepted.amount != str(PAYMENT_AMOUNT):
        raise DemoPaymentRejected("payment amount does not match the advertised quote")
    if int(accepted.max_timeout_seconds) != MAX_TIMEOUT_SECONDS:
        raise DemoPaymentRejected("payment timeout does not match the advertised quote")
    if accepted.extra != {"name": "USDC", "version": "2"}:
        raise DemoPaymentRejected("payment EIP-712 domain does not match the quote")

    exact = _authorization(payload)
    authorization = exact.authorization
    try:
        payer = Web3.to_checksum_address(authorization.from_address)
        authorization_to = Web3.to_checksum_address(authorization.to)
        value = int(authorization.value)
        valid_after = int(authorization.valid_after)
        valid_before = int(authorization.valid_before)
    except Exception as exc:
        raise DemoPaymentRejected("malformed EIP-3009 authorization") from exc
    if int(payer, 16) == 0 or payer in {
        Web3.to_checksum_address(PROVIDER),
        Web3.to_checksum_address(ASSURANCE_OPERATOR),
    }:
        raise DemoPaymentRejected("buyer must be a separate non-zero EOA")
    if authorization_to != Web3.to_checksum_address(PROVIDER):
        raise DemoPaymentRejected("authorization recipient does not match the quote")
    if value != PAYMENT_AMOUNT:
        raise DemoPaymentRejected("authorization amount does not match the quote")
    current_time = int(time.time()) if now is None else now
    if valid_after > current_time:
        raise DemoPaymentRejected("authorization is not yet valid")
    if valid_before < current_time + 6:
        raise DemoPaymentRejected("authorization is expired")
    if not _HEX_32.fullmatch(authorization.nonce):
        raise DemoPaymentRejected("authorization nonce must be 32 bytes")
    if not isinstance(exact.signature, str) or not _HEX_SIGNATURE.fullmatch(exact.signature):
        raise DemoPaymentRejected("authorization signature must be a 65-byte hex value")
    return exact


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            json.dump(value, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


class DemoSubmissionStore:
    """Minimal durable replay guard keyed only by a payload commitment."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def get(self, payload_hash: str) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text())
            record = data.get("records", {}).get(payload_hash)
            return None if record is None else dict(record)

    def put(self, payload_hash: str, record: Mapping[str, Any]) -> None:
        with self._lock:
            data = {"schema_version": "1", "records": {}}
            if self.path.exists():
                data = json.loads(self.path.read_text())
            data.setdefault("records", {})[payload_hash] = dict(record)
            _atomic_json(self.path, data)


class ArcEIP3009Settler:
    """Provider-funded Web3 relayer using x402's canonical EIP-3009 helpers."""

    def __init__(
        self,
        account: Any,
        *,
        w3: Any = None,
        retry_policy: GenLayerRetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: 0.0,
    ):
        self.account = account
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
        self.retry_policy = retry_policy or GenLayerRetryPolicy()
        self._sleep = sleep
        self._jitter = jitter
        self._scheme = ExactEvmScheme(self)
        if Web3.to_checksum_address(account.address) != Web3.to_checksum_address(PROVIDER):
            raise DemoProviderError("provider key does not derive the locked provider")
        chain_id = int(self._read("Arc chain ID", lambda: self.w3.eth.chain_id))
        if chain_id != ARC_TESTNET_CHAIN_ID:
            raise DemoProviderError(
                f"connected chain {chain_id} is not Arc Testnet {ARC_TESTNET_CHAIN_ID}"
            )

    def _read(self, label: str, operation: Callable[[], Any]) -> Any:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if not is_transient_transport_error(exc):
                    raise DemoProviderError(f"{label} failed: {exc}") from exc
                if attempt == self.retry_policy.max_attempts:
                    raise DemoProviderError(f"{label} transport retries exhausted") from exc
                self._sleep(self.retry_policy.delay_before_attempt(attempt + 1, self._jitter()))
        raise RuntimeError("unreachable retry state")

    def get_addresses(self) -> list[str]:
        return [Web3.to_checksum_address(self.account.address)]

    def get_chain_id(self) -> int:
        return int(self._read("Arc chain ID", lambda: self.w3.eth.chain_id))

    def get_code(self, address: str) -> bytes:
        return bytes(
            self._read(
                "Arc contract code",
                lambda: self.w3.eth.get_code(Web3.to_checksum_address(address)),
            )
        )

    def read_contract(
        self,
        address: str,
        abi: list[dict[str, Any]],
        function_name: str,
        *args: Any,
    ) -> Any:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        function = contract.get_function_by_name(function_name)(*args)
        return self._read(
            f"{function_name} simulation",
            lambda: function.call({"from": self.account.address}),
        )

    def verify_typed_data(self, *args: Any, **kwargs: Any) -> bool:
        # ExactEvmScheme uses strict EOA recovery directly. Contract-wallet
        # verification routes through read_contract, so this fallback is unused.
        return False

    def get_balance(self, address: str, token_address: str) -> int:
        abi = [
            {
                "type": "function",
                "name": "balanceOf",
                "stateMutability": "view",
                "inputs": [{"name": "account", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
            }
        ]
        return int(self.read_contract(token_address, abi, "balanceOf", address))

    def write_contract(
        self,
        address: str,
        abi: list[dict[str, Any]],
        function_name: str,
        *args: Any,
        data_suffix: str | None = None,
    ) -> str:
        if data_suffix:
            raise DemoProviderError("data suffixes are not supported by this demo")
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        function = contract.get_function_by_name(function_name)(*args)
        sender = Web3.to_checksum_address(self.account.address)
        nonce = int(
            self._read(
                "provider pending nonce",
                lambda: self.w3.eth.get_transaction_count(sender, "pending"),
            )
        )
        gas_price = int(self._read("Arc gas price", lambda: self.w3.eth.gas_price))
        estimate = int(
            self._read(
                "settlement gas estimate",
                lambda: function.estimate_gas({"from": sender}),
            )
        )
        transaction = self._read(
            "settlement transaction construction",
            lambda: function.build_transaction(
                {
                    "from": sender,
                    "nonce": nonce,
                    "chainId": ARC_TESTNET_CHAIN_ID,
                    "gas": math.ceil(estimate * GAS_LIMIT_MULTIPLIER),
                    "gasPrice": gas_price,
                }
            ),
        )
        signed = self.account.sign_transaction(transaction)
        try:
            raw_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            if is_transient_transport_error(exc):
                raise DemoSettlementOutcomeUnknown(
                    "Arc settlement broadcast outcome is unknown; blind resubmission is unsafe"
                ) from exc
            raise DemoProviderError(f"Arc settlement submission failed: {exc}") from exc
        tx_hash = raw_hash.hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        if len(tx_hash) != 66:
            raise DemoSettlementOutcomeUnknown(
                "Arc returned an unusable settlement transaction hash"
            )
        return tx_hash.lower()

    def send_transaction(self, to: str, data: bytes) -> str:
        raise DemoProviderError("smart-wallet deployment is outside this EOA demo")

    def wait_for_transaction_receipt(self, tx_hash: str) -> Mapping[str, Any]:
        return self.wait(tx_hash)

    def verify(self, payload: PaymentPayload) -> str:
        result = self._scheme.verify(payload, payment_requirements())
        if not result.is_valid or not result.payer:
            reason = result.invalid_reason or "invalid EIP-3009 authorization"
            raise DemoPaymentRejected(reason)
        return Web3.to_checksum_address(result.payer)

    def submit(self, payload: PaymentPayload) -> str:
        exact = _authorization(payload)
        parsed = parse_eip3009_authorization(exact.authorization)
        signature = bytes.fromhex((exact.signature or "").removeprefix("0x"))
        from x402.mechanisms.evm.erc6492 import parse_erc6492_signature

        signature_data = parse_erc6492_signature(signature)
        return execute_transfer_with_authorization(
            self,
            ARC_TESTNET_USDC,
            parsed,
            signature_data,
        )

    def wait(self, transaction_hash: str) -> Mapping[str, Any]:
        try:
            return self._read(
                "Arc settlement receipt",
                lambda: self.w3.eth.wait_for_transaction_receipt(transaction_hash, timeout=180),
            )
        except Exception as exc:
            raise DemoSettlementPending(transaction_hash) from exc

    def transfer_matches(self, receipt: Mapping[str, Any], payload: PaymentPayload) -> bool:
        exact = _authorization(payload)
        parsed = parse_eip3009_authorization(exact.authorization)
        return verify_eip3009_transfer_event(
            receipt.get("logs"),
            ARC_TESTNET_USDC,
            from_address=parsed.from_address,
            to=parsed.to,
            value=parsed.value,
        )


class AssuranceDemoProvider:
    """One-route x402 service with durable at-most-once settlement tracking."""

    def __init__(self, endpoint_url: str, settler: DemoSettler, store: DemoSubmissionStore):
        self.endpoint_url = endpoint_url
        self.settler = settler
        self.store = store
        self._settlement_lock = threading.Lock()

    def unpaid_response(self, *, error: str = "Payment required") -> DemoHTTPResponse:
        encoded = encode_payment_required_header(payment_required(self.endpoint_url, error=error))
        return DemoHTTPResponse(
            status=402,
            headers={
                "Content-Type": "application/json",
                PAYMENT_REQUIRED_HEADER: encoded,
                "Cache-Control": "no-store",
            },
            body=b"{}",
        )

    def handle(self, headers: Mapping[str, str]) -> DemoHTTPResponse:
        payment_header = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == PAYMENT_SIGNATURE_HEADER.lower()
            ),
            None,
        )
        if not payment_header:
            return self.unpaid_response()
        try:
            decoded = decode_payment_signature_header(payment_header)
            if not isinstance(decoded, PaymentPayload):
                raise DemoPaymentRejected("only x402 v2 is supported")
            settle = self._settle(decoded)
        except DemoSettlementPending as exc:
            return DemoHTTPResponse(
                status=503,
                headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
                body=json.dumps(
                    {"error": "settlement pending", "transaction": exc.transaction_hash},
                    sort_keys=True,
                ).encode(),
            )
        except DemoSettlementOutcomeUnknown:
            return DemoHTTPResponse(
                status=503,
                headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
                body=b'{"error":"settlement outcome unknown; do not resubmit"}',
            )
        except (DemoPaymentRejected, DemoSettlementFailed, ValueError) as exc:
            return self.unpaid_response(error=str(exc))
        return DemoHTTPResponse(
            status=200,
            headers={
                "Content-Type": "application/json",
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(settle),
                "Cache-Control": "no-store",
            },
            body=BAD_RESPONSE_BYTES,
        )

    def _settle(self, payload: PaymentPayload) -> SettleResponse:
        exact = validate_payment_payload(payload)
        payload_hash = _authorization_commitment(payload)
        with self._settlement_lock:
            record = self.store.get(payload_hash)
            if record and record.get("status") == "ambiguous":
                raise DemoSettlementOutcomeUnknown("authorization has an ambiguous prior broadcast")
            if record and record.get("status") == "failed":
                raise DemoSettlementFailed("authorization settlement previously failed")
            payer = Web3.to_checksum_address(exact.authorization.from_address)
            if record and record.get("status") == "confirmed":
                return SettleResponse(
                    success=True,
                    payer=payer,
                    transaction=record["transaction_hash"],
                    network=ARC_TESTNET_NETWORK,
                    amount=str(PAYMENT_AMOUNT),
                    extra={"asset": ARC_TESTNET_USDC, "payTo": PROVIDER},
                )
            if record and record.get("transaction_hash"):
                transaction_hash = record["transaction_hash"]
            else:
                verified_payer = self.settler.verify(payload)
                if Web3.to_checksum_address(verified_payer) != payer:
                    raise DemoPaymentRejected("verified payer does not match authorization")
                try:
                    transaction_hash = self.settler.submit(payload)
                except DemoSettlementOutcomeUnknown:
                    self.store.put(
                        payload_hash,
                        {"status": "ambiguous", "payer": payer, "updated_at": time.time()},
                    )
                    raise
                self.store.put(
                    payload_hash,
                    {
                        "status": "pending",
                        "payer": payer,
                        "transaction_hash": transaction_hash,
                        "updated_at": time.time(),
                    },
                )
                print(f"X402_SETTLEMENT_TX_HASH={transaction_hash}", flush=True)
            try:
                receipt = self.settler.wait(transaction_hash)
            except DemoSettlementPending:
                raise
            if int(receipt.get("status", 0)) != 1:
                self.store.put(
                    payload_hash,
                    {
                        "status": "failed",
                        "payer": payer,
                        "transaction_hash": transaction_hash,
                        "updated_at": time.time(),
                    },
                )
                raise DemoSettlementFailed("Arc settlement transaction reverted")
            if not self.settler.transfer_matches(receipt, payload):
                self.store.put(
                    payload_hash,
                    {
                        "status": "failed",
                        "payer": payer,
                        "transaction_hash": transaction_hash,
                        "updated_at": time.time(),
                    },
                )
                raise DemoSettlementFailed("settlement lacks the exact Arc USDC transfer")
            self.store.put(
                payload_hash,
                {
                    "status": "confirmed",
                    "payer": payer,
                    "transaction_hash": transaction_hash,
                    "block_number": int(receipt.get("blockNumber", 0)),
                    "updated_at": time.time(),
                },
            )
            return SettleResponse(
                success=True,
                payer=payer,
                transaction=transaction_hash,
                network=ARC_TESTNET_NETWORK,
                amount=str(PAYMENT_AMOUNT),
                extra={"asset": ARC_TESTNET_USDC, "payTo": PROVIDER},
            )
