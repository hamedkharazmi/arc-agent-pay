"""
interceptor.py — Layer 2 of arc-agent-pay: the x402 Payment Interceptor.

PaymentClient is a payment-aware wrapper around httpx.AsyncClient.
Any HTTP 402 response is caught, paid via the x402 Python SDK
(EIP-3009 + Circle Gateway), and retried — all invisible to the caller.
Budget enforcement runs before every payment attempt.

Dependencies:
    uv add "x402[httpx,evm]" eth-account

Usage (simplest form):
    from arc_agent_pay import PaymentClient
    from eth_account import Account

    account = Account.from_key(os.environ["AGENT_PRIVATE_KEY"])
    async with PaymentClient(account=account, budget_usdc="1.00") as client:
        response = await client.get("https://api.example.com/data")
        print(response.json())

How the 402 flow works (per x402 spec):
    1. Client sends GET /resource
    2. Server returns 402 + X-PAYMENT-REQUIRED header with payment terms
    3. x402 SDK reads requirements, signs EIP-3009 TransferWithAuthorization
       (offchain, zero gas)
    4. x402 SDK retries with X-PAYMENT header containing the signed payload
    5. Server verifies via Circle Gateway facilitator, returns 200 + resource
    6. Circle Gateway batches settlement onchain in the background

x402 SDK version note:
    Uses x402 >= 2.6 transport-based API. Event hooks were removed because
    httpx hooks cannot modify (replace) responses, which is required for
    the 402 → sign → retry flow.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import inspect
from decimal import Decimal
from typing import Any, Callable, Optional

from .budget import BudgetGuard
from .exceptions import (
    InsufficientFundsError,
    PaymentFailedError,
    PaymentPolicyError,
    PaymentStoreError,
    PaymentTimeoutError,
)
from .models import Chain, Payment, PaymentStatus
from .payment_store import (
    InMemoryPaymentStore,
    SqlitePaymentStore,
    default_payment_store_path,
)
from .policy import PaymentPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Network constants
# ---------------------------------------------------------------------------

# CAIP-2 chain identifiers
NETWORKS = {
    Chain.ARC_TESTNET:  "eip155:5042002",  # Arc Testnet chain ID (confirmed via RPC)
    Chain.BASE_SEPOLIA: "eip155:84532",
    Chain.BASE:         "eip155:8453",
    Chain.ETHEREUM:     "eip155:1",
}

# Register Arc Testnet in the x402 SDK (not included in the package's built-in configs)
def _register_arc_testnet() -> None:
    try:
        from x402.mechanisms.evm.constants import NETWORK_CONFIGS
        NETWORK_CONFIGS["eip155:5042002"] = {
            "chain_id": 5042002,
            "default_asset": {
                "address": "0x3600000000000000000000000000000000000000",
                "name": "USDC",
                "version": "2",
                "decimals": 6,
            },
        }
    except Exception:
        pass

_register_arc_testnet()

NETWORKS_REVERSE = {v: k for k, v in NETWORKS.items()}

# Arc Testnet RPC endpoint
ARC_TESTNET_RPC = "https://rpc.testnet.arc.network"


# ---------------------------------------------------------------------------
# Budget-enforcing transport
# ---------------------------------------------------------------------------

class _BudgetEnforcingTransport:
    """
    Custom httpx transport that adds budget enforcement to the x402 payment flow.

    Sits between httpx and the network. On a 402 response it:
      1. Parses the required payment amount
      2. Checks + reserves the amount from BudgetGuard (atomic, raises if over budget)
      3. Signs the EIP-3009 payment via the x402 SDK
      4. Retries the request with the X-PAYMENT header
      5. Marks the payment SUCCESS or FAILED, refunding on failure

    Using a transport (not event hooks) is required because httpx event hooks
    cannot replace the response object — only a transport can do that.
    """

    _RETRY_KEY = "_x402_is_retry"
    _PAYMENT_ID_KEY = "arc_agent_pay.payment_id"

    def __init__(
        self,
        x402_client: Any,          # x402.x402Client
        payment_client: "PaymentClient",
        underlying: Any = None,    # httpx.AsyncBaseTransport
    ) -> None:
        import httpx
        from x402.http.x402_http_client import x402HTTPClient

        self._x402_client = x402_client
        self._http_helper = x402HTTPClient(x402_client)
        self._payment_client = payment_client
        self._transport = underlying or httpx.AsyncHTTPTransport()
        self._emit = payment_client._on_event  # shorthand

    async def handle_async_request(self, request: Any) -> Any:
        is_retry = bool(request.extensions.get(self._RETRY_KEY))

        # --- idempotency: replay a cached paid response if enabled ---
        signature = self._signature(request)
        if self._payment_client._idem_ttl > 0 and not is_retry:
            cached = self._payment_client._idem_get(signature)
            if cached is not None:
                self._emit("payment_deduped", {"service": str(request.url)})
                return cached

        # --- initial request (with transient connection/5xx retries) ---
        response = await self._send_with_transient_retries(request)

        if response.status_code != 402:
            return response

        # Don't retry a retry (avoids infinite loops)
        if is_retry:
            return response

        await response.aread()

        # Parse the x402 terms before any signature or budget reservation.
        def _get_header(name: str) -> Optional[str]:
            return response.headers.get(name)

        body = None
        try:
            body = response.json()
        except Exception:
            pass

        try:
            payment_required = self._http_helper.get_payment_required_response(_get_header, body)
        except Exception as exc:
            raise PaymentFailedError(f"Failed to parse x402 payment requirements: {exc}") from exc

        requested_id = request.extensions.get(self._PAYMENT_ID_KEY)
        payment_id = self._payment_client._resolve_payment_id(requested_id)
        payment = self._payment_client._payment_from_required(
            request=request,
            response=response,
            payment_required=payment_required,
            payment_id=payment_id,
            request_key=signature,
        )
        amount_usdc = payment.amount_usdc

        self._emit("payment_required", {
            "payment_id": payment.payment_id,
            "service": payment.service_url,
            "amount_usdc": amount_usdc,
            "pay_to": payment.pay_to or "",
            "network": payment.network or self._payment_client.network,
            "asset": payment.asset or "",
        })

        try:
            reservation = self._payment_client._reserve_payment(payment)
        except PaymentPolicyError as exc:
            self._emit("policy_blocked", {
                "payment_id": payment.payment_id,
                "service": payment.service_url,
                "amount_usdc": amount_usdc,
                "reason": str(exc),
            })
            raise

        payment = reservation.payment
        if not reservation.is_new and not self._payment_client._supports_payment_identifier(
            payment_required
        ):
            raise PaymentPolicyError(
                f"seller does not advertise x402 payment-identifier support; "
                f"payment {payment.payment_id} cannot be resumed safely"
            )
        reserved_budget = False
        if reservation.is_new:
            try:
                self._payment_client.budget_guard.check_and_record(amount_usdc)
                reserved_budget = True
            except InsufficientFundsError as exc:
                payment.status = PaymentStatus.SKIPPED
                payment.error = str(exc)
                self._payment_client._save_payment(payment)
                self._emit("budget_blocked", {
                    "payment_id": payment.payment_id,
                    "service": payment.service_url,
                    "amount_usdc": amount_usdc,
                    "budget_remaining": self._payment_client.budget_guard.remaining,
                    "reason": str(exc),
                })
                raise

        self._payment_client._payments.append(payment)
        logger.info(
            f"[arc-agent-pay] paying {amount_usdc} USDC → {request.url} "
            f"| payment id: {payment.payment_id} "
            f"| budget remaining: {self._payment_client.budget_guard.remaining} USDC"
        )
        self._emit("payment_signing", {
            "payment_id": payment.payment_id,
            "service": payment.service_url,
            "amount_usdc": amount_usdc,
            "scheme": payment.scheme,
            "note": "Signing off-chain — zero gas",
        })

        # --- x402 sign + retry ---
        try:
            create_payment_payload = self._x402_client.create_payment_payload
            payload_kwargs = self._payment_client._payment_payload_kwargs(
                payment_required, payment.payment_id
            )
            if "extensions" not in inspect.signature(create_payment_payload).parameters:
                payload_kwargs = {}
            if inspect.iscoroutinefunction(create_payment_payload):
                payment_payload = await create_payment_payload(payment_required, **payload_kwargs)
            else:
                payment_payload = await asyncio.to_thread(
                    create_payment_payload,
                    payment_required,
                    **payload_kwargs,
                )
            payment_headers = self._http_helper.encode_payment_signature_header(payment_payload)
        except Exception as e:
            if reservation.is_new:
                if reserved_budget:
                    self._payment_client.budget_guard.refund(amount_usdc)
                payment.status = PaymentStatus.FAILED
                payment.error = str(e)
                self._payment_client._save_payment(payment)
            self._emit("payment_failed", {
                "payment_id": payment.payment_id,
                "service": payment.service_url,
                "amount_usdc": amount_usdc,
                "error": str(e),
            })
            raise PaymentFailedError(f"Failed to build x402 payment: {e}") from e

        if payment.status == PaymentStatus.PENDING:
            payment.status = PaymentStatus.AUTHORIZED
            self._payment_client._save_payment(payment)

        retry_headers = dict(payment_headers)
        retry_headers["Access-Control-Expose-Headers"] = "PAYMENT-RESPONSE,X-PAYMENT-RESPONSE"
        retry_request = self._clone(
            request,
            extra_headers=retry_headers,
            extensions={self._RETRY_KEY: True},
        )

        # The paid retry is single-shot — never auto-retried, to avoid the risk
        # of double settlement if a response is lost after the chain tx lands.
        try:
            retry_response = await self._transport.handle_async_request(retry_request)
        except Exception as exc:
            if payment.status != PaymentStatus.SUCCESS:
                payment.status = PaymentStatus.UNKNOWN
                payment.error = f"paid request outcome is unknown: {exc}"
                self._payment_client._save_payment(payment)
            self._emit("payment_unknown", {
                "payment_id": payment.payment_id,
                "service": payment.service_url,
                "amount_usdc": amount_usdc,
                "error": str(exc),
            })
            raise PaymentTimeoutError(
                f"Payment {payment.payment_id} may have settled; reuse this payment ID to retry safely"
            ) from exc

        # --- Update this request's own Payment record (not a list search, so
        #     concurrent in-flight payments are attributed correctly) ---
        payment.response_status = retry_response.status_code
        settlement = PaymentClient.extract_payment_response(retry_response.headers)
        tx_hash = str(settlement.get("transaction") or "")
        is_http_success = 200 <= retry_response.status_code < 300
        settlement_failed = settlement.get("success") is False
        if (is_http_success and not settlement_failed) or tx_hash:
            payment.status = PaymentStatus.SUCCESS
            payment.error = None if is_http_success else f"settled; HTTP {retry_response.status_code}"
            if tx_hash:
                payment.tx_reference = tx_hash
            self._payment_client._save_payment(payment)
            self._emit("payment_settled", {
                "payment_id": payment.payment_id,
                "service": payment.service_url,
                "amount_usdc": amount_usdc,
                "tx_hash": tx_hash,
                "explorer_url": f"https://explorer.testnet.arc.network/tx/{tx_hash}" if tx_hash else "",
                "budget_remaining": self._payment_client.budget_guard.remaining,
                "chain": payment.network or self._payment_client.network,
            })
            if self._payment_client._idem_ttl > 0 and is_http_success:
                await retry_response.aread()
                self._payment_client._idem_put(
                    signature,
                    retry_response.status_code,
                    retry_response.headers,
                    retry_response.content,
                )
        else:
            reason = await self._extract_failure_reason(retry_response)
            error = f"HTTP {retry_response.status_code} after payment"
            if reason:
                error = f"{error}: {reason}"
            # A generic 4xx can come from application code after settlement.
            # Only an explicit settlement failure or a repeated 402 proves that
            # the authorization was rejected and its reservation can be released.
            conclusive_rejection = retry_response.status_code == 402 or settlement_failed
            if reservation.is_new and conclusive_rejection:
                payment.status = PaymentStatus.FAILED
                payment.error = error
                if reserved_budget:
                    self._payment_client.budget_guard.refund(amount_usdc)
                logger.warning(
                    f"Payment failed for {payment.service_url} ({error}) — "
                    f"refunded {payment.amount_usdc} USDC to budget."
                )
                self._emit("payment_failed", {
                    "payment_id": payment.payment_id,
                    "service": payment.service_url,
                    "amount_usdc": amount_usdc,
                    "error": error,
                })
            elif payment.status != PaymentStatus.SUCCESS:
                payment.status = PaymentStatus.UNKNOWN
                payment.error = error
                self._emit("payment_unknown", {
                    "payment_id": payment.payment_id,
                    "service": payment.service_url,
                    "amount_usdc": amount_usdc,
                    "error": error,
                })
            self._payment_client._save_payment(payment)

        return retry_response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _extract_failure_reason(response: Any) -> str:
        """Best-effort extraction of the server's reason from a failed paid
        retry. x402 servers put the verify/settle error in the JSON body
        (e.g. {"error": ...} or x402's invalid_reason); fall back to text."""
        try:
            await response.aread()
            data = response.json()
        except Exception:
            try:
                text = (response.text or "").strip()
            except Exception:
                return ""
            return text[:200]
        if isinstance(data, dict):
            for key in ("error", "invalidReason", "invalid_reason", "reason", "detail", "message"):
                value = data.get(key)
                if value:
                    return str(value)[:200]
        return str(data)[:200]

    @staticmethod
    def _signature(request: Any) -> str:
        """Stable key for a request: method + url (incl. query) + body hash."""
        import hashlib

        body = request.content or b""
        return f"{request.method} {request.url}|{hashlib.sha256(body).hexdigest()}"

    def _clone(self, request: Any, *, extra_headers: Optional[dict] = None,
               extensions: Optional[dict] = None) -> Any:
        """Rebuild an identical request (safe to re-send after a failed attempt)."""
        import httpx

        headers = dict(request.headers)
        if extra_headers:
            headers.update(extra_headers)
        ext = dict(request.extensions)
        if extensions:
            ext.update(extensions)
        return httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=request.content,
            extensions=ext,
        )

    async def _send_with_transient_retries(self, request: Any) -> Any:
        """
        Send the pre-payment request, retrying transient failures with
        exponential backoff: connection/read errors and HTTP 5xx. The paid
        retry deliberately does NOT use this (single-shot, no double settle).
        """
        import asyncio

        import httpx

        attempts = self._payment_client._max_retries + 1
        for i in range(attempts):
            is_last = i == attempts - 1
            try:
                resp = await self._transport.handle_async_request(request)
            except httpx.TransportError:
                if is_last:
                    raise
                await asyncio.sleep(self._payment_client._retry_backoff * (2 ** i))
                request = self._clone(request)
                continue
            if resp.status_code >= 500 and not is_last:
                await resp.aread()
                await asyncio.sleep(self._payment_client._retry_backoff * (2 ** i))
                request = self._clone(request)
                continue
            return resp
        return resp  # pragma: no cover - loop always returns/raises above

    async def aclose(self) -> None:
        await self._transport.aclose()


# ---------------------------------------------------------------------------
# PaymentClient
# ---------------------------------------------------------------------------

class PaymentClient:
    """
    An async HTTP client that automatically handles HTTP 402 Payment Required
    responses using the x402 protocol and Circle Gateway nanopayments.

    Familiar async-httpx usage with automatic 402 interception, payment, and retry.
    Budget enforcement prevents overspending across concurrent requests.

    Payment semantics: at-most-once. The paid retry is single-shot and is never
    auto-retried, so a payment is never sent twice for one request; if the
    response is lost after the chain tx lands, the call surfaces as failed even
    though settlement occurred (EIP-3009's per-authorization nonce still prevents
    any on-chain replay). Enable `idempotency_ttl` to dedup identical repeated
    paid requests within a window (returns the cached response instead of paying
    again) — useful when a higher layer may retry the same call.

    Args:
        account:         eth_account.LocalAccount — holds the EVM private key
                         used to sign EIP-3009 payment authorizations.
        budget_usdc:     Max USDC spend for this session, e.g. "1.00".
        chain:           Which Arc/EVM chain to operate on.
        base_url:        Optional base URL prefix for all requests.
        timeout:         Request timeout in seconds.
        max_retries:     Transient-error retries (connection errors + 5xx) for
                         the pre-payment request only. Default 2.
        retry_backoff:   Base seconds for exponential backoff between retries.
        idempotency_ttl: Seconds to cache a successful paid response for replay
                         on identical repeat requests. 0 (default) disables it.
        policy:          Pre-signature host/network/recipient and rolling spend policy.
        payment_store:   PaymentStore journal. Rolling policies default to a
                         durable SQLite store when one is not supplied.
        asset_decimals:  Decimals used to present the selected atomic amount as
                         ``amount_usdc``. Arc USDC defaults to 6.
    """

    def __init__(
        self,
        account: Any = None,                      # eth_account.LocalAccount
        budget_usdc: str = "1.00",
        chain: Chain = Chain.ARC_TESTNET,
        base_url: str = "",
        timeout: float = 30.0,
        on_event: Optional[Callable[[str, dict], None]] = None,
        signer: Any = None,                       # x402 ClientEvmSigner
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        idempotency_ttl: float = 0.0,
        policy: Optional[PaymentPolicy] = None,
        payment_store: Any = None,
        asset_decimals: int = 6,
    ) -> None:
        if account is None and signer is None:
            raise ValueError("PaymentClient requires either account or signer.")
        self.account = account
        self.signer = signer
        self.chain = chain
        self.network = NETWORKS.get(chain, NETWORKS[Chain.ARC_TESTNET])
        self.base_url = base_url
        self.timeout = timeout
        self.asset_decimals = int(asset_decimals)
        if self.asset_decimals < 0:
            raise ValueError("asset_decimals must be non-negative")
        self.budget_guard = BudgetGuard(budget_usdc)
        self.policy = policy or PaymentPolicy()
        if payment_store is not None:
            self.payment_store = payment_store
        elif self.policy.has_rolling_limits:
            try:
                self.payment_store = SqlitePaymentStore(default_payment_store_path())
            except Exception as exc:
                if self.policy.fail_closed:
                    raise PaymentStoreError(f"could not initialize payment journal: {exc}") from exc
                logger.warning("could not initialize durable payment journal; using process memory")
                self.payment_store = InMemoryPaymentStore()
        else:
            self.payment_store = InMemoryPaymentStore()
        self._fallback_payment_store = InMemoryPaymentStore()
        self._payments: list[Payment] = []
        self._client: Any = None   # httpx.AsyncClient, set in __aenter__
        self.__on_event = on_event
        # Transient-retry config (connection errors + 5xx on the pre-payment GET).
        # The paid retry is never auto-retried, to avoid double settlement.
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff))
        # Opt-in idempotency: when ttl > 0, a successful paid response is cached by
        # request signature and replayed (without paying again) for repeats within
        # the window. Default 0 = disabled (at-most-once, no dedup).
        self._idem_ttl = max(0.0, float(idempotency_ttl))
        self._idem_cache: dict[str, tuple[float, int, list, bytes]] = {}

    def _on_event(self, event_type: str, payload: dict) -> None:
        if self.__on_event is not None:
            try:
                self.__on_event(event_type, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Idempotency cache (opt-in via idempotency_ttl)
    # ------------------------------------------------------------------

    def _idem_get(self, signature: str) -> Optional[Any]:
        """Return a fresh httpx.Response for a cached paid request, or None."""
        import time

        import httpx

        entry = self._idem_cache.get(signature)
        if entry is None:
            return None
        expiry, status, headers, content = entry
        if time.monotonic() > expiry:
            self._idem_cache.pop(signature, None)
            return None
        return httpx.Response(status_code=status, headers=headers, content=content)

    def _idem_put(self, signature: str, status: int, headers: Any, content: bytes) -> None:
        import time

        self._idem_cache[signature] = (
            time.monotonic() + self._idem_ttl,
            status,
            list(headers.items()),
            content,
        )

    # ------------------------------------------------------------------
    # Policy, journal, and protocol identity
    # ------------------------------------------------------------------

    def _reserve_payment(self, payment: Payment) -> Any:
        try:
            return self.payment_store.reserve(payment, self.policy)
        except PaymentPolicyError:
            raise
        except PaymentStoreError:
            if self.policy.fail_closed:
                raise
            logger.warning("payment journal unavailable; falling back to process memory")
            return self._fallback_payment_store.reserve(payment, self.policy)

    def _save_payment(self, payment: Payment) -> None:
        try:
            stored = self.payment_store.update(payment)
            payment.updated_at = stored.updated_at
        except PaymentStoreError:
            if self.policy.fail_closed:
                raise
            logger.warning("payment journal update failed; retaining a process-local record")
            existing = self._fallback_payment_store.get(payment.payment_id or "")
            if existing is None:
                self._fallback_payment_store.reserve(payment, PaymentPolicy())
            stored = self._fallback_payment_store.update(payment)
            payment.updated_at = stored.updated_at

    @staticmethod
    def _resolve_payment_id(requested: Any = None) -> str:
        from x402.extensions.payment_identifier import generate_payment_id, is_valid_payment_id

        payment_id = str(requested).strip() if requested is not None else generate_payment_id()
        if not is_valid_payment_id(payment_id):
            raise ValueError(
                "payment_id must be 16-128 characters containing only letters, numbers, '-' or '_'"
            )
        return payment_id

    @staticmethod
    def _requirement_value(requirement: Any, *names: str) -> Any:
        for name in names:
            if isinstance(requirement, dict) and name in requirement:
                return requirement[name]
            if hasattr(requirement, name):
                return getattr(requirement, name)
        return None

    def _select_x402_requirement(self, _version: int, requirements: list[Any]) -> Any:
        """Choose a requirement on the client's configured chain.

        The same selector is supplied to upstream x402 and called locally before
        signing, so policy accounting always describes the authorization that is
        actually created.
        """
        if not requirements:
            raise PaymentFailedError("402 response did not include payment requirements")

        network_matches = [
            req for req in requirements
            if self._requirement_value(req, "network") == self.network
        ]
        if network_matches:
            candidates = network_matches
        else:
            declared = [self._requirement_value(req, "network") for req in requirements]
            if any(declared):
                raise PaymentFailedError(
                    f"service does not accept payments on configured network {self.network}"
                )
            candidates = list(requirements)  # compatibility with incomplete v1/test fixtures

        exact = [
            req for req in candidates
            if self._requirement_value(req, "scheme") in (None, "exact")
        ]
        return (exact or candidates)[0]

    def _payment_from_required(
        self,
        *,
        request: Any,
        response: Any,
        payment_required: Any,
        payment_id: str,
        request_key: str,
    ) -> Payment:
        accepts = self._requirement_value(payment_required, "accepts") or []
        selected = self._select_x402_requirement(2, list(accepts)) if accepts else None

        raw_amount = None
        if selected is not None:
            getter = getattr(selected, "get_amount", None)
            raw_amount = getter() if callable(getter) else self._requirement_value(
                selected, "amount", "max_amount_required", "maxAmountRequired"
            )
        if raw_amount is None:
            amount_usdc = self._parse_amount_from_402(response)
            if amount_usdc is None:
                raise PaymentFailedError("402 response did not include a valid payment amount")
            raw_amount = str(Decimal(amount_usdc) * (Decimal(10) ** self.asset_decimals))
        else:
            try:
                amount_usdc = str(
                    Decimal(str(raw_amount)) / (Decimal(10) ** self.asset_decimals)
                )
            except Exception as exc:
                raise PaymentFailedError(f"invalid atomic payment amount {raw_amount!r}") from exc

        def value(*names: str) -> Any:
            return self._requirement_value(selected, *names) if selected else None

        return Payment(
            payment_id=payment_id,
            request_key=request_key,
            method=request.method,
            service_url=str(request.url),
            amount_usdc=amount_usdc,
            amount_atomic=str(raw_amount),
            chain=self.chain,
            status=PaymentStatus.PENDING,
            scheme=str(value("scheme") or "exact"),
            network=str(value("network") or self.network),
            asset=value("asset"),
            pay_to=value("pay_to", "payTo", "pay_to_address", "payToAddress")
            or self._parse_pay_to_from_402(response)
            or None,
        )

    @staticmethod
    def _payment_payload_kwargs(payment_required: Any, payment_id: str) -> dict[str, Any]:
        """Return x402 v2 extension kwargs carrying the standard payment ID."""
        version = PaymentClient._requirement_value(
            payment_required, "x402_version", "x402Version"
        )
        if version not in (None, 2):
            return {}
        declared = PaymentClient._requirement_value(payment_required, "extensions")
        if not declared:
            return {}
        extensions = copy.deepcopy(declared)
        from x402.extensions.payment_identifier import append_payment_identifier_to_extensions

        append_payment_identifier_to_extensions(extensions, payment_id)
        return {"extensions": extensions}

    @staticmethod
    def _supports_payment_identifier(payment_required: Any) -> bool:
        declared = PaymentClient._requirement_value(payment_required, "extensions") or {}
        if not isinstance(declared, dict):
            return False
        from x402.extensions.payment_identifier import is_payment_identifier_extension

        return is_payment_identifier_extension(declared.get("payment-identifier"))

    # ------------------------------------------------------------------
    # Context manager — builds the httpx client with x402 transport
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PaymentClient":
        try:
            import httpx
            from x402 import x402Client, x402ClientSync
            from x402.mechanisms.evm.signers import EthAccountSigner
            from x402.mechanisms.evm.exact.register import register_exact_evm_client
        except ImportError as e:
            raise ImportError(
                "Missing x402 dependencies.\n"
                'Install with: uv add "x402[httpx,evm]" eth-account'
            ) from e

        # Delegate-signing flows may provide a custom signer that blocks while
        # waiting for an out-of-process signature; use the sync client in that
        # path and execute payload creation in a worker thread.
        x402_client_cls = x402ClientSync if self.signer is not None else x402Client
        x402_client = x402_client_cls(
            payment_requirements_selector=self._select_x402_requirement
        )
        resolved_signer = self.signer or EthAccountSigner(self.account)
        register_exact_evm_client(x402_client, resolved_signer)

        # The transport owns all transient retries (connection errors + 5xx) on
        # the pre-payment request, so the underlying transport uses no retries.
        transport = _BudgetEnforcingTransport(x402_client, self)

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 402 response parsing
    # ------------------------------------------------------------------

    def _parse_amount_from_402(self, response: Any) -> Optional[str]:
        """
        Extract the payment amount (as USDC decimal string) from a 402 response.

        x402 v2 spec encodes payment requirements in the X-PAYMENT-REQUIRED
        header as base64 JSON. The amount field (maxAmountRequired) is in
        raw token units — USDC has 6 decimals, so 1000 = 0.001 USDC.

        Falls back to parsing the response body if the header is absent.
        """
        # x402 v2 uses PAYMENT-REQUIRED; v1/compat servers use X-PAYMENT-REQUIRED
        header = (
            response.headers.get("PAYMENT-REQUIRED")
            or response.headers.get("X-PAYMENT-REQUIRED", "")
        )
        data = self._decode_x402_header(header)

        if not data:
            try:
                data = response.json()
            except Exception:
                pass

        if not data:
            return None

        accepts = data.get("accepts", [])
        if not accepts or not isinstance(accepts, list):
            return None

        # x402 v2.10+ uses "amount"; earlier versions used "maxAmountRequired"
        raw = accepts[0].get("amount") or accepts[0].get("maxAmountRequired", "")
        if not raw:
            return None

        try:
            return str(Decimal(str(raw)) / Decimal("1000000"))
        except Exception:
            return None

    def _parse_pay_to_from_402(self, response: Any) -> str:
        """Extract the recipient (payTo) address from a 402 response, or ""."""
        header = (
            response.headers.get("PAYMENT-REQUIRED")
            or response.headers.get("X-PAYMENT-REQUIRED", "")
        )
        data = self._decode_x402_header(header)
        if not data:
            try:
                data = response.json()
            except Exception:
                return ""

        accepts = data.get("accepts", []) if isinstance(data, dict) else []
        if not accepts or not isinstance(accepts, list):
            return ""
        return str(accepts[0].get("payTo") or accepts[0].get("payToAddress") or "")

    @staticmethod
    def _decode_x402_header(header: str) -> Optional[dict]:
        """Try base64 decode then raw JSON parse of an x402 header value."""
        if not header:
            return None
        try:
            decoded = base64.b64decode(header + "==").decode()
            return json.loads(decoded)
        except Exception:
            pass
        try:
            return json.loads(header)
        except Exception:
            return None

    @staticmethod
    def extract_payment_response(headers: Any) -> dict[str, Any]:
        """Decode the x402 settlement response header, returning ``{}`` on absence/error."""
        raw = headers.get("PAYMENT-RESPONSE") or headers.get("X-PAYMENT-RESPONSE", "")
        if not raw:
            return {}
        try:
            value = json.loads(base64.b64decode(raw + "==").decode())
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def extract_tx_hash(headers: Any) -> str:
        """
        Pull the settlement tx hash from a paid 200 response.

        x402 v2 returns it in the PAYMENT-RESPONSE header (base64 JSON with a
        "transaction" field); v1/compat servers use X-PAYMENT-RESPONSE. Returns
        "" if absent or unparseable.
        """
        return str(PaymentClient.extract_payment_response(headers).get("transaction") or "")

    # ------------------------------------------------------------------
    # HTTP methods (delegate to httpx client)
    # ------------------------------------------------------------------

    def _assert_open(self) -> None:
        if self._client is None:
            raise RuntimeError(
                "PaymentClient must be used as an async context manager:\n"
                "  async with PaymentClient(...) as client:\n"
                "      response = await client.get(url)"
            )

    async def request(
        self,
        method: str,
        url: str,
        *,
        payment_id: Optional[str] = None,
        **kwargs,
    ) -> Any:
        """Send an HTTP request, optionally resuming a logical x402 payment ID."""
        self._assert_open()
        extensions = dict(kwargs.pop("extensions", {}) or {})
        if payment_id is not None:
            # Validate before sending the free probe request, so a malformed ID
            # cannot produce a side effect before the caller sees the error.
            extensions[_BudgetEnforcingTransport._PAYMENT_ID_KEY] = self._resolve_payment_id(
                payment_id
            )
        return await self._client.request(method, url, extensions=extensions, **kwargs)

    async def get(self, url: str, **kwargs) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> Any:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> Any:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> Any:
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> Any:
        return await self.request("PATCH", url, **kwargs)

    async def head(self, url: str, **kwargs) -> Any:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs) -> Any:
        return await self.request("OPTIONS", url, **kwargs)

    # ------------------------------------------------------------------
    # Session inspection
    # ------------------------------------------------------------------

    @property
    def payments(self) -> list[Payment]:
        """All payment records for this session."""
        return list(self._payments)

    @property
    def total_spent(self) -> str:
        """Total USDC spent this session."""
        return self.budget_guard.spent

    def summary(self) -> dict:
        """Full session summary: budget state + all payment records."""
        return {
            "budget": self.budget_guard.summary(),
            "payment_count": len(self._payments),
            "successful_payments": sum(
                1 for p in self._payments if p.status == PaymentStatus.SUCCESS
            ),
            "payments": [p.model_dump() for p in self._payments],
        }
