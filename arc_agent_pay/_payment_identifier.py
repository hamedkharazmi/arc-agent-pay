"""Dependency-light x402 payment-identifier wire helpers.

x402 2.15-2.18 exposes these helpers through ``x402.extensions``, whose package
initializer also imports optional Bazaar validation dependencies.  The core
arc-agent-pay install should not need JSON Schema tooling just to attach the
standard identifier object, so this module implements the small published wire
contract locally.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

PAYMENT_IDENTIFIER = "payment-identifier"
PAYMENT_ID_MIN_LENGTH = 16
PAYMENT_ID_MAX_LENGTH = 128
PAYMENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def generate_payment_id(prefix: str = "pay_") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def is_valid_payment_id(payment_id: Any) -> bool:
    return (
        isinstance(payment_id, str)
        and PAYMENT_ID_MIN_LENGTH <= len(payment_id) <= PAYMENT_ID_MAX_LENGTH
        and PAYMENT_ID_PATTERN.fullmatch(payment_id) is not None
    )


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True)
        return dumped if isinstance(dumped, dict) else None
    return None


def is_payment_identifier_extension(extension: Any) -> bool:
    extension_dict = _as_dict(extension)
    if not extension_dict:
        return False
    info = _as_dict(extension_dict.get("info"))
    return bool(info) and isinstance(info.get("required"), bool)


def append_payment_identifier_to_extensions(
    extensions: dict[str, Any], payment_id: str
) -> dict[str, Any]:
    extension = extensions.get(PAYMENT_IDENTIFIER)
    if not is_payment_identifier_extension(extension):
        return extensions
    if not is_valid_payment_id(payment_id):
        raise ValueError(f"invalid x402 payment identifier {payment_id!r}")

    extension_dict = dict(_as_dict(extension) or {})
    info = dict(_as_dict(extension_dict.get("info")) or {})
    info["id"] = payment_id
    extension_dict["info"] = info
    extensions[PAYMENT_IDENTIFIER] = extension_dict
    return extensions
