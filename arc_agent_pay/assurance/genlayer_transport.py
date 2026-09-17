"""Bounded Studio-dev retries with a strict transaction submission boundary.

The pinned GenLayer SDK funnels both its custom RPC methods and Web3 calls
through ``GenLayerProvider.make_request``.  Wrapping that single method lets us
retry proven read-only/preparation calls without ever retrying
``eth_sendRawTransaction``.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import socket
import ssl
import time
from typing import Any, Callable, Mapping, Optional

from ..exceptions import (
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
)


SAFE_STUDIO_DEV_RPC_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_estimateGas",
        "eth_getBalance",
        "eth_getTransactionByHash",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
        "gen_call",
        "gen_getTransactionLifecycle",
        "sim_call",
        "sim_estimateTransactionFees",
        "sim_getConsensusContract",
        "sim_getFeeConfig",
    }
)
SUBMISSION_RPC_METHOD = "eth_sendRawTransaction"

_TRANSIENT_SSL_MARKERS = (
    "eof",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "unexpected_eof",
)
_NON_TRANSIENT_SSL_MARKERS = (
    "certificate verify failed",
    "hostname mismatch",
    "certificate has expired",
)


@dataclass(frozen=True)
class GenLayerRetryPolicy:
    """Conservative bounded retry policy for safe Studio-dev requests."""

    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0
    max_jitter_seconds: float = 0.125

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.max_jitter_seconds < 0:
            raise ValueError("retry jitter must be non-negative")

    def delay_before_attempt(self, next_attempt: int, jitter: float) -> float:
        """Return the delay before a one-indexed retry attempt."""
        exponent = max(0, next_attempt - 2)
        backoff = min(
            self.base_delay_seconds * (2**exponent),
            self.max_delay_seconds,
        )
        return backoff + min(max(jitter, 0.0), self.max_jitter_seconds)


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        if next_error is None:
            reason = getattr(current, "reason", None)
            next_error = reason if isinstance(reason, BaseException) else None
        current = next_error


def _requests_transport_types() -> tuple[type[BaseException], ...]:
    try:
        from requests import exceptions as requests_exceptions
    except ImportError:
        return ()
    return (
        requests_exceptions.ConnectionError,
        requests_exceptions.Timeout,
    )


def _requests_ssl_type() -> tuple[type[BaseException], ...]:
    try:
        from requests import exceptions as requests_exceptions
    except ImportError:
        return ()
    return (requests_exceptions.SSLError,)


def _urllib3_transport_types() -> tuple[type[BaseException], ...]:
    try:
        from urllib3 import exceptions as urllib3_exceptions
    except ImportError:
        return ()
    return (
        urllib3_exceptions.ConnectTimeoutError,
        urllib3_exceptions.NewConnectionError,
        urllib3_exceptions.ProtocolError,
        urllib3_exceptions.ReadTimeoutError,
    )


def _urllib3_ssl_type() -> tuple[type[BaseException], ...]:
    try:
        from urllib3 import exceptions as urllib3_exceptions
    except ImportError:
        return ()
    return (urllib3_exceptions.SSLError,)


def is_transient_transport_error(error: BaseException) -> bool:
    """Recognize connection failures, never JSON-RPC/application errors."""
    chain = tuple(_exception_chain(error))
    combined = " ".join(str(item).lower() for item in chain)
    if any(marker in combined for marker in _NON_TRANSIENT_SSL_MARKERS):
        return False

    requests_transport = _requests_transport_types()
    requests_ssl = _requests_ssl_type()
    urllib3_transport = _urllib3_transport_types()
    urllib3_ssl = _urllib3_ssl_type()
    for item in chain:
        if isinstance(item, (ssl.SSLEOFError, ConnectionResetError, socket.timeout)):
            return True
        if isinstance(item, TimeoutError):
            return True
        if (requests_ssl and isinstance(item, requests_ssl)) or (
            urllib3_ssl and isinstance(item, urllib3_ssl)
        ):
            if any(marker in combined for marker in _TRANSIENT_SSL_MARKERS):
                return True
            continue
        if requests_transport and isinstance(item, requests_transport):
            return True
        if urllib3_transport and isinstance(item, urllib3_transport):
            return True
    return False


class RetryingStudioDevProvider:
    """Callable installed over one SDK provider's ``make_request`` method."""

    def __init__(
        self,
        make_request: Callable[..., Any],
        *,
        policy: GenLayerRetryPolicy,
        sleep: Callable[[float], None],
        jitter: Callable[[], float],
    ) -> None:
        self._make_request = make_request
        self.policy = policy
        self._sleep = sleep
        self._jitter = jitter
        self.broadcast_transaction_hash: Optional[str] = None

    def begin_submission(self) -> None:
        self.broadcast_transaction_hash = None

    def finish_submission(self) -> None:
        self.broadcast_transaction_hash = None

    def __call__(self, method: Any, params: list[Any]) -> Any:
        method_name = str(method)
        if method_name == SUBMISSION_RPC_METHOD:
            try:
                response = self._make_request(method, params)
            except Exception as exc:
                if is_transient_transport_error(exc):
                    raise GenLayerSubmissionOutcomeUnknown(method_name) from exc
                raise
            if isinstance(response, Mapping):
                transaction_hash = response.get("result")
                if transaction_hash is not None:
                    self.broadcast_transaction_hash = str(transaction_hash)
            return response

        if method_name not in SAFE_STUDIO_DEV_RPC_METHODS:
            return self._make_request(method, params)

        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                return self._make_request(method, params)
            except Exception as exc:
                if not is_transient_transport_error(exc):
                    raise
                if attempt == self.policy.max_attempts:
                    raise GenLayerTransportRetryExhausted(
                        method_name,
                        attempt,
                    ) from exc
                delay = self.policy.delay_before_attempt(
                    attempt + 1,
                    self._jitter(),
                )
                self._sleep(delay)
        raise RuntimeError("unreachable retry state")


def install_studio_dev_retries(
    client: Any,
    *,
    policy: Optional[GenLayerRetryPolicy] = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Optional[Callable[[], float]] = None,
) -> RetryingStudioDevProvider:
    """Install the retry boundary once on a pinned GenLayer client."""
    provider = client.provider
    existing = getattr(provider, "_agentpay_retrying_make_request", None)
    if isinstance(existing, RetryingStudioDevProvider):
        return existing
    selected_policy = policy or GenLayerRetryPolicy()
    jitter_source = jitter or (
        lambda: random.uniform(0.0, selected_policy.max_jitter_seconds)
    )
    wrapper = RetryingStudioDevProvider(
        provider.make_request,
        policy=selected_policy,
        sleep=sleep,
        jitter=jitter_source,
    )
    provider.make_request = wrapper
    provider._agentpay_retrying_make_request = wrapper
    return wrapper


def _retry_wrapper(client: Any) -> Optional[RetryingStudioDevProvider]:
    provider = getattr(client, "provider", None)
    wrapper = getattr(provider, "_agentpay_retrying_make_request", None)
    return wrapper if isinstance(wrapper, RetryingStudioDevProvider) else None


def begin_submission(client: Any) -> None:
    wrapper = _retry_wrapper(client)
    if wrapper is not None:
        wrapper.begin_submission()


def finish_submission(client: Any) -> None:
    wrapper = _retry_wrapper(client)
    if wrapper is not None:
        wrapper.finish_submission()


def submission_error(error: BaseException, client: Any) -> Optional[BaseException]:
    """Classify a failed SDK submission without ever attempting it again."""
    for item in _exception_chain(error):
        if isinstance(item, GenLayerSubmissionOutcomeUnknown):
            return item
    wrapper = _retry_wrapper(client)
    if wrapper is not None and wrapper.broadcast_transaction_hash is not None:
        return GenLayerSubmissionOutcomeUnknown(
            SUBMISSION_RPC_METHOD,
            evm_transaction_hash=wrapper.broadcast_transaction_hash,
        )
    exhausted = retry_exhausted_error(error)
    if exhausted is not None:
        return exhausted
    if is_transient_transport_error(error):
        return GenLayerSubmissionOutcomeUnknown(SUBMISSION_RPC_METHOD)
    return None


def retry_exhausted_error(
    error: BaseException,
) -> Optional[GenLayerTransportRetryExhausted]:
    """Recover the typed retry error from SDK wrapper exceptions."""
    return next(
        (
            item
            for item in _exception_chain(error)
            if isinstance(item, GenLayerTransportRetryExhausted)
        ),
        None,
    )


__all__ = [
    "GenLayerRetryPolicy",
    "RetryingStudioDevProvider",
    "SAFE_STUDIO_DEV_RPC_METHODS",
    "SUBMISSION_RPC_METHOD",
    "begin_submission",
    "finish_submission",
    "install_studio_dev_retries",
    "is_transient_transport_error",
    "retry_exhausted_error",
    "submission_error",
]
