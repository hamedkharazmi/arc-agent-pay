"""
Tests for arc_agent_pay.llm — provider selection + completion behaviour.
Network calls are mocked (respx / monkeypatch); no real LLM is hit.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from arc_agent_pay.llm import (
    ArcAPIsProvider,
    OpenAIProvider,
    get_provider,
    synthesize_report,
)


# ---------------------------------------------------------------------------
# Test environment guards
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_env_providers(monkeypatch):
    """Prevent .env side effects from forcing real provider calls in tests."""
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# get_provider() selection order: ARCAPIS_TOKEN_ID -> OPENAI_API_KEY -> None
# ---------------------------------------------------------------------------

def test_get_provider_prefers_arcapis(monkeypatch):
    monkeypatch.setenv("ARCAPIS_TOKEN_ID", "tok123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
    provider = get_provider()
    assert isinstance(provider, ArcAPIsProvider)
    assert provider.name == "arcapis"


def test_get_provider_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
    provider = get_provider()
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai"


def test_get_provider_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert get_provider() is None


# ---------------------------------------------------------------------------
# ArcAPIsProvider.complete
# ---------------------------------------------------------------------------

@respx.mock
async def test_arcapis_complete_signs_and_parses_response():
    # Gateway wraps the upstream (OpenAI chat) response under `result`.
    route = respx.post("https://arcapis.com/api/call/24").mock(
        return_value=httpx.Response(
            200,
            json={
                "packet_id": "pk_24",
                "calls_remaining": 349,
                "calls_total": 350,
                "endpoint": "gpt5",
                "result": {"choices": [{"message": {"content": "hello from arc"}}]},
            },
        )
    )
    # Random key — we only assert the request is well-formed, not ownership.
    provider = ArcAPIsProvider(
        token_id="pk_24",
        signer_key="0x" + "11" * 32,
    )
    out = await provider.complete("say hi")
    assert out == "hello from arc"
    assert route.called

    req = route.calls.last.request
    assert req.headers["X-Call-Signature"].startswith("0x")
    # Auth header is base64(JSON) binding this packet.
    import base64 as _b64
    import json as _json

    msg = _json.loads(_b64.b64decode(req.headers["X-Call-Auth"]))
    assert msg["packetId"] == "24"
    assert msg["endpointId"] == "gpt5"
    assert msg["requestHash"].startswith("0x")
    # requestHash must bind the exact body bytes that were POSTed.
    from eth_utils import keccak

    assert msg["requestHash"] == "0x" + keccak(req.content).hex()

    # Packet quota is captured for the UI/telemetry.
    assert provider.last_call == {
        "packet_id": "pk_24",
        "calls_remaining": 349,
        "calls_total": 350,
        "cached": False,
        "endpoint": "gpt5",
    }


async def test_arcapis_requires_token():
    provider = ArcAPIsProvider(token_id=None, signer_key="0x" + "11" * 32)
    with pytest.raises(ValueError):
        await provider.complete("x")


async def test_arcapis_requires_signer_key(monkeypatch):
    monkeypatch.delenv("ARCAPIS_SIGNER_KEY", raising=False)
    monkeypatch.delenv("AGENT_PRIVATE_KEY", raising=False)
    provider = ArcAPIsProvider(token_id="pk_24")
    with pytest.raises(ValueError):
        await provider.complete("x")


# ---------------------------------------------------------------------------
# OpenAIProvider.complete (mock the openai client)
# ---------------------------------------------------------------------------

async def test_openai_complete(monkeypatch):
    class _Msg:
        content = "openai report"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **_kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.chat = _Chat()

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    provider = OpenAIProvider(api_key="sk-test")
    assert await provider.complete("prompt") == "openai report"


# ---------------------------------------------------------------------------
# synthesize_report fallback
# ---------------------------------------------------------------------------

async def test_synthesize_report_template_fallback(monkeypatch):
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = {"Price Feed": {"data": {"BTC": {"price_usd": 100000, "change_24h_pct": 2.5}}}}
    report = await synthesize_report("crypto", data)
    assert "Research Report: crypto" in report
    assert "Market Prices" in report


async def test_synthesize_report_uses_provider():
    class _StubProvider:
        name = "stub"

        async def complete(self, prompt: str, **opts) -> str:
            return "stub synthesized report"

    report = await synthesize_report("topic", {"svc": {"x": 1}}, provider=_StubProvider())
    assert report == "stub synthesized report"
