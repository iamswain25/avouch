"""Minimal resource server showing the native-ALGO x402 flow.

    GET /paid            -> 402 + accepts (asset "ALGO") until a valid payment lands
    GET /free            -> 200, no payment

Run: python server/x402_native/demo_server.py --pay-to <ADDRESS> --port 8851
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verifier import NativeX402  # noqa: E402

PRICE_MICRO_ALGO = 1_000


def make_handler(x402: NativeX402):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the test output readable
            if os.environ.get("X402_VERBOSE"):
                super().log_message(fmt, *args)

        def _send(self, code: int, body: dict, headers: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path == "/free":
                self._send(200, {"ok": True, "resource": "/free"})
                return
            if self.path != "/paid":
                self._send(404, {"error": "not found"})
                return

            header = self.headers.get("PAYMENT-SIGNATURE") or self.headers.get("X-PAYMENT")
            if not header:
                ch = x402.new_challenge("/paid", PRICE_MICRO_ALGO, "native ALGO payment demo")
                self._send(402, x402.payment_required(ch))
                return

            try:
                receipt = x402.verify_and_settle(header, "/paid")
            except ValueError as exc:  # expected: bad payment
                ch = x402.new_challenge("/paid", PRICE_MICRO_ALGO, "native ALGO payment demo")
                body = x402.payment_required(ch)
                body["error"] = str(exc)
                self._send(402, body)
                return
            except Exception as exc:  # unexpected: never drop the connection
                self._send(500, {"error": f"internal: {exc}"})
                return

            payload = base64.b64encode(json.dumps(receipt).encode()).decode()
            self._send(200, {"ok": True, "resource": "/paid", "receipt": receipt},
                       headers={"PAYMENT-RESPONSE": payload})

    return Handler


def serve(pay_to: str, port: int = 8851, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    x402 = NativeX402(pay_to)
    server = ThreadingHTTPServer((host, port), make_handler(x402))
    server.x402 = x402  # type: ignore[attr-defined]
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pay-to", required=True)
    ap.add_argument("--port", type=int, default=8851)
    args = ap.parse_args()
    server = serve(args.pay_to, args.port)
    print(f"listening on http://127.0.0.1:{args.port} (pay to {args.pay_to})")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
