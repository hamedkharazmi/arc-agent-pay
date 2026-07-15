"""
llm/arcapis_provider.py — ArcAPIs-backed LLMProvider (EIP-712 per-call auth).

ArcAPIs (arcapis.com) runs LLM inference paid on-chain via an Arc Testnet NFT
"packet" (tokenId). It is a single-prompt completion endpoint — it does NOT
expose a chat/tool-calling API, so it is only suitable for the synthesis step,
not the agent's tool-calling loop.

Authentication is per-call EIP-712: every request signs a short-lived
`CallAuth` message binding packetId + endpointId + requestHash + nonce +
validUntil. The gateway recovers the signer and requires it to be the on-chain
owner of the packet NFT (ownerOf). So the signing key MUST own the packet —
transfer the packet to your agent wallet, or set ARCAPIS_SIGNER_KEY to the
owner's key. A leaked signature is good for exactly one call, not the quota.

Reverse-engineered from github.com/manhcuongsev/Arcapis
(sdk/typescript/src/eip712.ts + netlify/functions/call.js, _eip712.js).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from typing import Any, Optional

# EIP-712 schema — must match the server's _eip712.js exactly.
ARCAPIS_HOST = "https://arcapis.com"
ARC_CHAIN_ID = 5042002
PACKET_CONTRACT = "0xB7B66bC873A4e9D415c1C4C4a8876AEC3892f798"

CALL_AUTH_DOMAIN = {
    "name": "Arcapis Call Auth",
    "version": "1",
    "chainId": ARC_CHAIN_ID,
    "verifyingContract": PACKET_CONTRACT,
}

CALL_AUTH_TYPES = {
    "CallAuth": [
        {"name": "packetId", "type": "uint256"},
        {"name": "endpointId", "type": "string"},
        {"name": "requestHash", "type": "bytes32"},
        {"name": "nonce", "type": "bytes32"},
        {"name": "validUntil", "type": "uint256"},
    ]
}


def _parse_packet_id(raw: str) -> Optional[int]:
    """Accept "pk_24", "24", or 24 → the numeric uint256 tokenId the gateway signs."""
    if raw is None:
        return None
    m = re.fullmatch(r"(?:pk_)?(\d+)", str(raw).strip())
    return int(m.group(1)) if m else None


class ArcAPIsProvider:
    """Completes prompts via arcapis.com on-chain inference (USDC-settled)."""

    name = "arcapis"

    def __init__(
        self,
        token_id: Optional[str] = None,
        signer_key: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        host: str = ARCAPIS_HOST,
        timeout: float = 60.0,
        validity_seconds: int = 120,
    ) -> None:
        self.packet_id = _parse_packet_id(token_id or os.environ.get("ARCAPIS_TOKEN_ID", ""))
        # Signer must own the packet NFT. Falls back to the agent's payment key.
        self.signer_key = (
            signer_key
            or os.environ.get("ARCAPIS_SIGNER_KEY")
            or os.environ.get("AGENT_PRIVATE_KEY")
        )
        # endpointId is signed into the message; "gpt5" for the OpenAI GPT-5 packet.
        self.endpoint_id = endpoint_id or os.environ.get("ARCAPIS_ENDPOINT_ID", "gpt5")
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.validity_seconds = validity_seconds
        # Quota metadata from the most recent call (for UI/telemetry). Populated
        # by complete(); None until the first successful call.
        self.last_call: Optional[dict[str, Any]] = None

    async def complete(self, prompt: str, **opts) -> str:
        if not self.packet_id:
            raise ValueError("ArcAPIsProvider requires ARCAPIS_TOKEN_ID (e.g. pk_24).")
        if not self.signer_key:
            raise ValueError(
                "ArcAPIsProvider requires a signing key (ARCAPIS_SIGNER_KEY or "
                "AGENT_PRIVATE_KEY) that owns the packet NFT."
            )

        import httpx
        from eth_account import Account
        from eth_utils import keccak

        # The body we hash MUST be the exact bytes we POST — the gateway recomputes
        # keccak256(utf8(body)) and compares it against the signed requestHash.
        body_string = json.dumps({"prompt": prompt})
        request_hash = keccak(text=body_string)  # bytes32
        nonce = secrets.token_bytes(32)
        valid_until = int(time.time()) + self.validity_seconds

        message = {
            "packetId": self.packet_id,
            "endpointId": self.endpoint_id,
            "requestHash": request_hash,
            "nonce": nonce,
            "validUntil": valid_until,
        }
        signed = Account.sign_typed_data(
            self.signer_key,
            domain_data=CALL_AUTH_DOMAIN,
            message_types=CALL_AUTH_TYPES,
            message_data=message,
        )

        # X-Call-Auth carries the message as base64(JSON), with bytes32 fields as
        # 0x-hex and packetId stringified (matches the SDK's buildAuthHeader).
        auth_header = base64.b64encode(
            json.dumps(
                {
                    "packetId": str(self.packet_id),
                    "endpointId": self.endpoint_id,
                    "requestHash": "0x" + request_hash.hex(),
                    "nonce": "0x" + nonce.hex(),
                    "validUntil": valid_until,
                }
            ).encode()
        ).decode()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.host}/api/call/{self.packet_id}",
                headers={
                    "Content-Type": "application/json",
                    "X-Call-Signature": signed.signature.to_0x_hex(),
                    "X-Call-Auth": auth_header,
                },
                content=body_string,  # raw bytes — must equal what we hashed
            )
            r.raise_for_status()
            data = r.json()

        # Capture packet quota so callers can surface it (UI, logs).
        if isinstance(data, dict):
            # Gateway returns an internal "chain:24" key for on-chain packets —
            # normalize to the friendly "pk_24" form for display.
            raw_id = str(data.get("packet_id") or "")
            if raw_id.startswith("chain:"):
                raw_id = "pk_" + raw_id.split(":", 1)[1]
            self.last_call = {
                "packet_id": raw_id or f"pk_{self.packet_id}",
                "calls_remaining": data.get("calls_remaining"),
                "calls_total": data.get("calls_total"),
                "cached": bool(data.get("_cached")),
                "endpoint": data.get("endpoint") or self.endpoint_id,
            }

        return _extract_text(data)


def _extract_text(data: Any) -> str:
    """Pull the completion text out of an ArcAPIs gateway response.

    The gateway wraps the upstream provider response under `result` (same shape
    on cache hits). For the GPT packets that's an OpenAI chat completion:
    result.choices[0].message.content. Older/other shapes are handled too.
    """
    result = data.get("result", data) if isinstance(data, dict) else data

    if isinstance(result, dict):
        # OpenAI chat: choices[0].message.content
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict) and msg.get("content"):
                return msg["content"]
        # Anthropic: content[0].text
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            if content[0].get("text"):
                return content[0]["text"]
        # Flat fallbacks (legacy / template responses)
        for key in ("response", "text", "content", "result"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val

    return str(result)
