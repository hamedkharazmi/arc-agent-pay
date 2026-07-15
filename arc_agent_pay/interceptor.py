"""
interceptor.py — Layer 2 of arc-agent-pay: the x402 Payment Interceptor.

PaymentClient is a drop-in replacement for httpx.AsyncClient.
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
import json
import logging
import inspect
from decimal import Decimal
from typing import Any, Callable, Optional

from .budget import BudgetGuard
from .exceptions import InsufficientFundsError, PaymentFailedError
from .models import Chain, Payment, PaymentStatus

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

        # --- Budget enforcement ---
        amount_usdc = self._payment_client._parse_amount_from_402(response)
        payment: Optional[Payment] = None

        if amount_usdc is not None:
            self._emit("payment_required", {
                "service": str(request.url),
                "amount_usdc": amount_usdc,
                "pay_to": self._payment_client._parse_pay_to_from_402(response),
                "network": self._payment_client.network,
            })
            try:
                self._payment_client.budget_guard.check_and_record(amount_usdc)
            except InsufficientFundsError as exc:
                logger.error(
                    f"Budget exhausted — payment blocked for {request.url}. "
                    f"Need {amount_usdc} USDC, "
                    f"{self._payment_client.budget_guard.remaining} remaining."
                )
                self._emit("budget_blocked", {
                    "service": str(request.url),
                    "amount_usdc": amount_usdc,
                    "budget_remaining": self._payment_client.budget_guard.remaining,
                    "reason": str(exc),
                })
                raise

            payment = Payment(
                service_url=str(request.url),
                amount_usdc=amount_usdc,
                chain=self._payment_client.chain,
                status=PaymentStatus.PENDING,
            )
            self._payment_client._payments.append(payment)

            logger.info(
                f"[arc-agent-pay] paying {amount_usdc} USDC → {request.url} "
                f"| budget remaining: {self._payment_client.budget_guard.remaining} USDC"
            )
            self._emit("payment_signing", {
                "service": str(request.url),
                "amount_usdc": amount_usdc,
                "scheme": "EIP-3009 TransferWithAuthorization",
                "note": "Signing off-chain — zero gas",
            })

        # --- x402 sign + retry ---
        def _get_header(name: str) -> Optional[str]:
            return response.headers.get(name)

        body = None
        try:
            body = response.json()
        except Exception:
            pass

        try:
            payment_required = self._http_helper.get_payment_required_response(_get_header, body)
            create_payment_payload = self._x402_client.create_payment_payload
            if inspect.iscoroutinefunction(create_payment_payload):
                payment_payload = await create_payment_payload(payment_required)
            else:
                payment_payload = await asyncio.to_thread(
                    create_payment_payload,
                    payment_required,
                )
            payment_headers = self._http_helper.encode_payment_signature_header(payment_payload)
        except Exception as e:
            if payment is not None:
                self._payment_client.budget_guard.refund(amount_usdc)
                payment.status = PaymentStatus.FAILED
                payment.error = str(e)
                self._emit("payment_failed", {
                    "service": str(request.url),
                    "amount_usdc": amount_usdc,
                    "error": str(e),
                })
            raise PaymentFailedError(f"Failed to build x402 payment: {e}") from e

        retry_headers = dict(payment_headers)
        retry_headers["Access-Control-Expose-Headers"] = "PAYMENT-RESPONSE,X-PAYMENT-RESPONSE"
        retry_request = self._clone(
            request,
            extra_headers=retry_headers,
            extensions={self._RETRY_KEY: True},
        )

        # The paid retry is single-shot — never auto-retried, to avoid the risk
        # of double settlement if a response is lost after the chain tx lands.
        retry_response = await self._transport.handle_async_request(retry_request)

        # --- Update this request's own Payment record (not a list search, so
        #     concurrent in-flight payments are attributed correctly) ---
        if payment is not None:
            if retry_response.status_code == 200:
                payment.status = PaymentStatus.SUCCESS
                tx_hash = PaymentClient.extract_tx_hash(retry_response.headers)
                if tx_hash:
                    payment.tx_reference = tx_hash
                self._emit("payment_settled", {
                    "service": str(request.url),
                    "amount_usdc": amount_usdc,
                    "tx_hash": tx_hash,
                    "explorer_url": f"https://explorer.testnet.arc.network/tx/{tx_hash}" if tx_hash else "",
                    "budget_remaining": self._payment_client.budget_guard.remaining,
                    "chain": "Arc Testnet",
                })
                if self._payment_client._idem_ttl > 0:
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
                payment.status = PaymentStatus.FAILED
                payment.error = error
                self._payment_client.budget_guard.refund(amount_usdc)
                logger.warning(
                    f"Payment failed for {payment.service_url} ({error}) — "
                    f"refunded {payment.amount_usdc} USDC to budget."
                )
                self._emit("payment_failed", {
                    "service": str(request.url),
                    "amount_usdc": amount_usdc,
                    "error": error,
                })

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

    Drop-in usage — works exactly like httpx.AsyncClient for the caller.
    All 402 responses are intercepted, paid, and retried transparently.
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
    ) -> None:
        if account is None and signer is None:
            raise ValueError("PaymentClient requires either account or signer.")
        self.account = account
        self.signer = signer
        self.chain = chain
        self.network = NETWORKS.get(chain, NETWORKS[Chain.ARC_TESTNET])
        self.base_url = base_url
        self.timeout = timeout
        self.budget_guard = BudgetGuard(budget_usdc)
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
        x402_client = x402ClientSync() if self.signer is not None else x402Client()
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
    def extract_tx_hash(headers: Any) -> str:
        """
        Pull the settlement tx hash from a paid 200 response.

        x402 v2 returns it in the PAYMENT-RESPONSE header (base64 JSON with a
        "transaction" field); v1/compat servers use X-PAYMENT-RESPONSE. Returns
        "" if absent or unparseable.
        """
        raw = headers.get("PAYMENT-RESPONSE") or headers.get("X-PAYMENT-RESPONSE", "")
        if not raw:
            return ""
        try:
            data = json.loads(base64.b64decode(raw + "==").decode())
            return data.get("transaction", "")
        except Exception:
            return ""

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

    async def get(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.post(url, **kwargs)

    async def put(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.put(url, **kwargs)

    async def delete(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.delete(url, **kwargs)

    async def patch(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.patch(url, **kwargs)

    async def head(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.head(url, **kwargs)

    async def options(self, url: str, **kwargs) -> Any:
        self._assert_open()
        return await self._client.options(url, **kwargs)

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
