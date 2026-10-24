"""Avouch board over MCP (Streamable HTTP, JSON-RPC 2.0) with native-ALGO payment.

Why this exists: an agent that speaks MCP should not need to know our HTTP shape.
It gets tools, and when a tool costs money the result carries the payment
requirement in `structuredContent` (x402's MCP convention: isError true plus a
PaymentRequired object), then retries the same call with `_meta["x402/payment"]`.

Deliberately plain: stdlib only, one POST endpoint, no session state beyond the
payment challenge store. Endpoint: POST /mcp

Run: python server/mcp/avouch_mcp.py --pay-to <ADDRESS> --port 8853
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "x402_native"))
from verifier import NativeX402  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "avouch-board", "version": "0.1.0"}

# prices in microALGO (1 ALGO = 1_000_000)
READ_PRICE = 1_000        # ~$0.0001
CREATE_FEE = 20_000       # ~$0.0021
MIN_BOND = 1_000_000      # 1 ALGO, peg rule: ceil(target_usd / ALGO_USD)

PAYMENT_INSTRUCTIONS = {
    "scheme": "exact-algo",
    "standard": "NOT x402-compatible: x402 exact settles ASA/SPL/ERC-20 tokens only, this settles native ALGO",
    "network": "algorand (CAIP-2 in the 402 response)",
    "envelope": {
        "x402Version": 2,
        "scheme": "exact",
        "network": "<from accepts[].network>",
        "payload": {"txn": "<base64 msgpack SignedTransaction of a native pay>"},
    },
    "transaction_rules": [
        "type must be pay, single ungrouped transaction",
        "receiver must equal accepts[].payTo",
        "amount must be >= accepts[].maxAmountRequired",
        "note must be exactly accepts[].extra.paymentId (ASCII hex)",
        "fee must be <= 2 * 1000 microALGO",
        "rekey_to and close_remainder_to must be empty",
        "transaction must not be expired when the server settles it",
    ],
    "error_codes": {
        "underpaid": "amount below maxAmountRequired",
        "receiver mismatch": "receiver is not payTo",
        "unknown or expired paymentId": "paymentId missing from note or challenge expired",
        "paymentId already used": "single use; request a fresh challenge",
        "transaction expired": "last_valid_round passed before settlement",
        "settlement failed": "network rejected the transaction (bad signature, overspend)",
    },
    "client_steps": [
        "call the tool without payment -> read structuredContent.accepts[0]",
        "build a native pay: payTo, maxAmountRequired, note = extra.paymentId",
        "retry the same tool call with _meta['x402/payment'] = base64(envelope)",
        "read structuredContent.receipt for the settled txid",
    ],
}

TOOLS = [
    {
        "name": "board_index",
        "description": "Free index of the board: posts with id, title, content hash, bond, status. No payment.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "post_read",
        "description": (
            "Read one post body. Paid: the first call returns isError=true with a PaymentRequired "
            "object (native ALGO, scheme exact-algo). Pay, then retry with the payment in _meta."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"post_id": {"type": "string", "description": "post id from board_index"}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "post_create",
        "description": (
            "Create a post. Returns the atomic group to sign: fee to the treasury, bond to the app "
            "escrow, then post_bond. Paid: the fee leg is a native-ALGO payment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_hash": {"type": "string", "minLength": 17},
                "window_seconds": {"type": "integer", "minimum": 60},
            },
            "required": ["content_hash", "window_seconds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "post_challenge",
        "description": "Challenge a post inside its window. Returns the challenger bond requirement and the app call to sign.",
        "inputSchema": {
            "type": "object",
            "properties": {"post_id": {"type": "string"}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "payment_instructions",
        "description": (
            "Machine-readable description of the exact-algo payment scheme: envelope, transaction rules, "
            "error codes, client steps. Free. Read this before paying anything."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

# demo state: a single seed post so tools return something without a database
BOARD = {
    "1": {
        "id": "1",
        "title": "Why a bond, not a fee",
        "content_hash": "sha256:9f2c1d7a4b8e5f60718293a4b5c6d7e8f901234567890abcdef1234567890ab",
        "bond_micro_algo": MIN_BOND,
        "status": "OPEN",
        "body": "A fee is final, so it only raises the price of spam. A bond is returned, so the cost of a bad post is its expected loss, not its price.",
    }
}


class Board:
    def __init__(self, x402: NativeX402):
        self.x402 = x402

    # ------------------------------------------------------------------ tools

    def call(self, name: str, args: dict, payment_header: str | None):
        if name == "payment_instructions":
            return self._ok(PAYMENT_INSTRUCTIONS, "exact-algo payment instructions")

        if name == "board_index":
            rows = [{k: v for k, v in p.items() if k != "body"} for p in BOARD.values()]
            return self._ok({"posts": rows, "count": len(rows), "read_price_micro_algo": READ_PRICE},
                            f"{len(rows)} posts")

        if name == "post_read":
            post_id = str(args.get("post_id", ""))
            post = BOARD.get(post_id)
            if post is None:
                return self._err(f"unknown post_id {post_id}", {"error": "unknown post"})
            resource = f"/mcp/post_read/{post_id}"
            if not payment_header:
                return self._payment_required(resource, READ_PRICE, "read post body")
            return self._settle_and(payment_header, resource, {"post": post})

        if name == "post_create":
            content_hash = str(args.get("content_hash", ""))
            window = int(args.get("window_seconds", 0))
            if len(content_hash) < 17 or window < 60:
                return self._err("content_hash must be >=17 chars and window_seconds >= 60", {})
            resource = "/mcp/post_create"
            if not payment_header:
                return self._payment_required(resource, CREATE_FEE, "post fee")
            plan = {
                "atomic_group": [
                    {"index": 0, "txn": "pay", "from": "author", "to": "treasury", "amount_micro_algo": CREATE_FEE},
                    {"index": 1, "txn": "pay", "from": "author", "to": "app_escrow", "amount_micro_algo": MIN_BOND},
                    {"index": 2, "txn": "appl", "method": "post_bond(pay,uint64,byte[],uint64)",
                     "args": ["<group index 1>", "<post_id>", content_hash, str(window)]},
                ],
                "note": "the fee leg (index 0) is the payment already settled for this call; sign the group with the same author account",
            }
            return self._settle_and(payment_header, resource, plan)

        if name == "post_challenge":
            post_id = str(args.get("post_id", ""))
            post = BOARD.get(post_id)
            if post is None:
                return self._err(f"unknown post_id {post_id}", {})
            required = post["bond_micro_algo"]  # 1:1 in this demo
            return self._ok({
                "post_id": post_id,
                "challenger_bond_micro_algo": required,
                "atomic_group": [
                    {"index": 0, "txn": "pay", "from": "challenger", "to": "app_escrow", "amount_micro_algo": required},
                    {"index": 1, "txn": "appl", "method": "challenge(pay,uint64)", "args": ["<group index 0>", post_id]},
                ],
            }, "challenge plan ready")

        return self._err(f"unknown tool {name}", {"error": "unknown tool"})

    # --------------------------------------------------------------- helpers

    def _payment_required(self, resource: str, amount: int, what: str):
        ch = self.x402.new_challenge(resource, amount, what)
        req = self.x402.payment_required(ch)
        req["error"] = "payment required"
        req["how_to_pay"] = {"tool": "payment_instructions", "scheme": "exact-algo"}
        return {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps(req, indent=1)}],
            "structuredContent": req,
        }

    def _settle_and(self, header: str, resource: str, payload: dict):
        try:
            receipt = self.x402.verify_and_settle(header, resource)
        except ValueError as exc:
            ch = self.x402.new_challenge(resource, READ_PRICE, "retry")
            req = self.x402.payment_required(ch)
            req["error"] = str(exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"payment rejected: {exc}"}],
                "structuredContent": req,
            }
        out = dict(payload)
        out["receipt"] = receipt
        return self._ok(out, f"settled {receipt['transaction']}")

    @staticmethod
    def _ok(payload: dict, text: str):
        return {
            "content": [{"type": "text", "text": f"{text}\n{json.dumps(payload, indent=1)}"}],
            "structuredContent": payload,
        }

    @staticmethod
    def _err(message: str, payload: dict):
        return {"isError": True, "content": [{"type": "text", "text": message}], "structuredContent": payload}


def make_handler(board: Board):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "avouch-mcp/0.1"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            if os.environ.get("MCP_VERBOSE"):
                super().log_message(format, *args)

        def _json(self, code: int, body: dict, extra: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path in ("/mcp", "/"):
                self._json(405, {"error": "use POST for MCP Streamable HTTP"}, {"Allow": "POST"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                msg = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                return

            method = msg.get("method")
            mid = msg.get("id")

            if method == "initialize":
                self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "Paid tool surface. Costly tools return isError=true with a PaymentRequired object "
                        "(scheme exact-algo, native ALGO, NOT x402-compatible). Read payment_instructions, "
                        "pay, then retry with _meta['x402/payment']."
                    ),
                }}, {"Mcp-Session-Id": uuid.uuid4().hex})

            elif method in ("notifications/initialized", "notifications/cancelled"):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            elif method == "ping":
                self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {}})

            elif method == "tools/list":
                self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})

            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                meta = params.get("_meta") or {}
                header = meta.get("x402/payment")
                result = board.call(name, args, header)
                self._json(200, {"jsonrpc": "2.0", "id": mid, "result": result})

            else:
                self._json(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": -32601, "message": f"method not found: {method}"}})

    return Handler


def serve(pay_to: str, port: int = 8853, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    x402 = NativeX402(pay_to)
    board = Board(x402)
    server = ThreadingHTTPServer((host, port), make_handler(board))
    server.board = board  # type: ignore[attr-defined]
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pay-to", required=True)
    ap.add_argument("--port", type=int, default=8853)
    args = ap.parse_args()
    server = serve(args.pay_to, args.port)
    print(f"MCP endpoint: http://127.0.0.1:{args.port}/mcp (pay to {args.pay_to})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
