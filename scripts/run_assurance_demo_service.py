#!/usr/bin/env python3
"""Run the localhost x402 provider for the AgentPay Assurance demo."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any

from eth_account import Account

from arc_agent_pay.assurance.demo_provider import (
    ARC_TESTNET_CHAIN_ID,
    PAYMENT_AMOUNT,
    PROVIDER,
    ArcEIP3009Settler,
    AssuranceDemoProvider,
    DemoSubmissionStore,
    demo_service,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8402
DEFAULT_STATE = Path.home() / ".local" / "state" / "agentpay" / "assurance-demo-provider.json"


def _provider_account() -> Any:
    private_key = os.environ.get("ASSURANCE_PROVIDER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("ASSURANCE_PROVIDER_PRIVATE_KEY is required")
    try:
        return Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError("ASSURANCE_PROVIDER_PRIVATE_KEY is invalid") from exc


def _handler(provider: AssuranceDemoProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            if self.path != "/assurance-demo":
                self.send_error(404)
                return
            content_length = int(self.headers.get("content-length", "0"))
            if content_length:
                self.rfile.read(content_length)
            response = provider.handle(dict(self.headers.items()))
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = _serve
        do_POST = _serve

        def log_message(self, format: str, *args: Any) -> None:
            print(f"assurance-demo http: {format % args}", flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("the MVP demo provider may bind only to localhost")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    account = _provider_account()
    endpoint = f"http://127.0.0.1:{args.port}/assurance-demo"
    settler = ArcEIP3009Settler(account)
    provider = AssuranceDemoProvider(
        endpoint,
        settler,
        DemoSubmissionStore(args.state),
    )
    print(
        json.dumps(
            {
                "chain_id": ARC_TESTNET_CHAIN_ID,
                "endpoint": endpoint,
                "provider": PROVIDER,
                "price_atomic": PAYMENT_AMOUNT,
                "state_path": str(args.state),
                "service": demo_service(endpoint).model_dump(mode="json"),
            },
            indent=2,
        ),
        flush=True,
    )
    server = ThreadingHTTPServer((args.host, args.port), _handler(provider))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
