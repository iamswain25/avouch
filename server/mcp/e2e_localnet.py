"""End-to-end MCP test: an MCP-only agent discovers the board, pays, and reads.

Flow
  initialize -> tools/list -> tools/call post_read (no payment)
  -> isError=true with PaymentRequired in structuredContent
  -> build a native-ALGO payment from those fields
  -> tools/call post_read again with _meta["x402/payment"]
  -> result + receipt; replaying the same payment is rejected

Run: .venv/bin/python server/mcp/e2e_localnet.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "x402_native"))

from algosdk import account, encoding, transaction  # noqa: E402
from algosdk.kmd import KMDClient  # noqa: E402
from algosdk.v2client import algod  # noqa: E402

from avouch_mcp import READ_PRICE, serve  # noqa: E402

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
PORT = 8853
URL = f"http://127.0.0.1:{PORT}/mcp"


def enc_txn(signed) -> str:
    """payload.txn must be base64 of the msgpack bytes (normalized across SDK versions)."""
    v = encoding.msgpack_encode(signed)
    try:
        if base64.b64decode(v, validate=True)[:1] in (b"\x81", b"\x82", b"\x83", b"\x84"):
            return v
    except Exception:
        pass
    return base64.b64encode(v.encode("latin-1")).decode()


def connect():
    a = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return a, sk, account.address_from_private_key(sk)


class McpClient:
    """Minimal MCP Streamable-HTTP client: one POST per JSON-RPC message."""

    def __init__(self, url: str):
        self.url = url
        self.session = None
        self._id = 0

    def rpc(self, method: str, params: dict | None = None):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                           "params": params or {}}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream",
                                              **({"Mcp-Session-Id": self.session} if self.session else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            if self.session is None:
                self.session = r.headers.get("Mcp-Session-Id")
            return json.loads(r.read().decode())

    def notify(self, method: str):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Mcp-Session-Id": self.session} if self.session else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status


def pay_from_accept(a: algod.AlgodClient, payer_sk: str, accept: dict) -> str:
    sender = account.address_from_private_key(payer_sk)
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.PaymentTxn(sender, sp, accept["payTo"], int(accept["maxAmountRequired"]),
                                 note=accept["extra"]["paymentId"].encode())
    signed = txn.sign(payer_sk)
    envelope = {
        "x402Version": 2,
        "scheme": accept["scheme"],
        "network": accept["network"],
        "payload": {"txn": enc_txn(signed)},
    }
    return base64.b64encode(json.dumps(envelope).encode()).decode()


def main() -> int:
    a, dispenser_sk, dispenser = connect()
    payer_sk, payer = account.generate_account()
    sp = a.suggested_params()
    fund = transaction.PaymentTxn(dispenser, sp, payer, 5_000_000)
    transaction.wait_for_confirmation(a, a.send_transaction(fund.sign(dispenser_sk)), 10)

    server = serve(dispenser, PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)

    results = []
    client = McpClient(URL)
    try:
        init = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "foreign-agent", "version": "1.0"}})
        info = init["result"]
        results.append(("1 initialize", bool(client.session) and info["capabilities"]["tools"] is not None,
                        f"session={str(client.session)[:8]} server={info['serverInfo']['name']}"))
        client.notify("notifications/initialized")

        tools = client.rpc("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        results.append(("2 tools/list", "post_read" in names and "payment_instructions" in names, str(names)))

        # unpaid call
        unpaid = client.rpc("tools/call", {"name": "post_read", "arguments": {"post_id": "1"}})["result"]
        req = unpaid.get("structuredContent") or {}
        accept = (req.get("accepts") or [{}])[0]
        unpaid_ok = (unpaid.get("isError") is True and accept.get("asset") == "ALGO"
                     and accept.get("maxAmountRequired") == str(READ_PRICE)
                     and bool((accept.get("extra") or {}).get("paymentId")))
        results.append(("3 unpaid call -> PaymentRequired", unpaid_ok,
                        f"asset={accept.get('asset')} amount={accept.get('maxAmountRequired')}"))

        # pay and retry
        balance_before = a.account_info(dispenser)["amount"]
        header = pay_from_accept(a, payer_sk, accept)
        paid = client.rpc("tools/call", {"name": "post_read", "arguments": {"post_id": "1"},
                                         "_meta": {"x402/payment": header}})["result"]
        receipt = (paid.get("structuredContent") or {}).get("receipt") or {}
        balance_after = a.account_info(dispenser)["amount"]
        paid_ok = (paid.get("isError") is not True and receipt.get("success") is True
                   and balance_after - balance_before == READ_PRICE
                   and "body" in (paid.get("structuredContent") or {}).get("post", {}))
        results.append(("4 paid retry -> body + receipt", paid_ok,
                        f"txid={str(receipt.get('transaction'))[:12]} delta={balance_after - balance_before}"))

        # replay
        replay = client.rpc("tools/call", {"name": "post_read", "arguments": {"post_id": "1"},
                                           "_meta": {"x402/payment": header}})["result"]
        results.append(("5 replay rejected", replay.get("isError") is True,
                        (replay.get("structuredContent") or {}).get("error")))

        # instructions tool is free and self-describing
        ins = client.rpc("tools/call", {"name": "payment_instructions", "arguments": {}})["result"]
        sc = ins.get("structuredContent") or {}
        results.append(("6 payment_instructions", sc.get("scheme") == "exact-algo" and "NOT x402" in sc.get("standard", ""),
                        sc.get("scheme")))

        # create plan carries the three-transaction group
        create_unpaid = client.rpc("tools/call", {"name": "post_create",
                                                  "arguments": {"content_hash": "sha256:test1234567890abc", "window_seconds": 3600}})["result"]
        acc2 = ((create_unpaid.get("structuredContent") or {}).get("accepts") or [{}])[0]
        header2 = pay_from_accept(a, payer_sk, acc2)
        created = client.rpc("tools/call", {"name": "post_create",
                                            "arguments": {"content_hash": "sha256:test1234567890abc", "window_seconds": 3600},
                                            "_meta": {"x402/payment": header2}})["result"]
        group = (created.get("structuredContent") or {}).get("atomic_group") or []
        fee = (created.get("structuredContent") or {}).get("fee_settled") or {}
        results.append(("7 post_create -> 2-txn group, fee settled once",
                        len(group) == 2 and group[1]["method"].startswith("post_bond")
                        and fee.get("in_this_call") is True,
                        f"{len(group)} legs, fee {fee.get('micro_algo')}"))
    finally:
        server.shutdown()

    print()
    ok_all = True
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name, "|", detail)
        ok_all &= bool(ok)
    print()
    print("RESULT:", "ALL PASS" if ok_all else "FAILURES PRESENT")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
