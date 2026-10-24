"""End-to-end test of the edge deployment (wrangler pages dev + localnet algod).

Covers the REST surface and the MCP surface against the same D1 database and the
same payment verifier, on a real chain (algokit localnet).

Prereq:
  wrangler pages dev --port 8789 --binding X402_SECRET=... --binding PAY_TO=<addr> \
    --binding ALGOD_URL=http://localhost:4001 --binding ALGOD_TOKEN=<token> ...

Run: .venv/bin/python scripts/e2e_worker_localnet.py [--base http://127.0.0.1:8789]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

from algosdk import account, encoding, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
NETWORK = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe"


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


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode()
            return r.status, dict(r.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:300]}
        return e.code, dict(e.headers), parsed


def pay_header(a, sk, accept):
    sender = account.address_from_private_key(sk)
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.PaymentTxn(sender, sp, accept["payTo"], int(accept["maxAmountRequired"]),
                                 note=accept["extra"]["paymentId"].encode())
    signed = txn.sign(sk)
    env = {"x402Version": 2, "scheme": accept["scheme"], "network": accept["network"],
           "payload": {"txn": enc_txn(signed)}}
    return base64.b64encode(json.dumps(env).encode()).decode(), signed.transaction.get_txid()


class Mcp:
    def __init__(self, base):
        self.url = f"{base}/mcp"
        self.session = None
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        status, hdrs, body = http("POST", self.url, {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}, headers)
        self.session = self.session or hdrs.get("Mcp-Session-Id")
        return status, body

    def tool(self, name, args=None, payment=None):
        params = {"name": name, "arguments": args or {}}
        if payment:
            params["_meta"] = {"x402/payment": payment}
        return self.call("tools/call", params)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8789")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    a, dispenser_sk, dispenser = connect()
    payer_sk, payer = account.generate_account()
    sp = a.suggested_params()
    fund = transaction.PaymentTxn(dispenser, sp, payer, 10_000_000)
    transaction.wait_for_confirmation(a, a.send_transaction(fund.sign(dispenser_sk)), 10)

    results = []
    stamp = str(int(time.time()))
    body_text = f"edge deploy body {stamp}"
    content_hash = "sha256:" + hashlib.sha256(body_text.encode()).hexdigest()

    # 1 free index
    st, _, body = http("GET", f"{base}/api/posts")
    results.append(("1 GET /api/posts (free)", st == 200 and "posts" in body, f"{st} count={body.get('count')}"))

    # 2 MCP handshake
    mcp = Mcp(base)
    st, body = mcp.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "edge-test", "version": "1"}})
    ok = st == 200 and body.get("result", {}).get("serverInfo", {}).get("name") == "avouch-board" and mcp.session
    results.append(("2 MCP initialize + session", bool(ok), f"{st} session={str(mcp.session)[:8]}"))
    mcp.call("notifications/initialized")

    # 3 tools/list
    st, body = mcp.call("tools/list")
    names = [t["name"] for t in body.get("result", {}).get("tools", [])]
    results.append(("3 MCP tools/list", st == 200 and len(names) == 5, str(names)))

    # 4 unpaid create -> 402
    st, _, unpaid = http("POST", f"{base}/api/posts", {"title": "edge", "body": body_text, "content_hash": content_hash, "window_seconds": 3600})
    acc = (unpaid.get("accepts") or [{}])[0]
    results.append(("4 POST /api/posts unpaid -> 402", st == 402 and acc.get("asset") == "ALGO", f"{st} amount={acc.get('maxAmountRequired')}"))

    # 5 pay the fee -> post created
    hdr, fee_txid = pay_header(a, payer_sk, acc)
    st, _, created = http("POST", f"{base}/api/posts", {"title": "edge", "body": body_text, "content_hash": content_hash, "window_seconds": 3600},
                          {"PAYMENT-SIGNATURE": hdr})
    post_id = created.get("post_id")
    legs = created.get("atomic_group") or []
    results.append(("5 POST /api/posts paid -> created", st == 200 and bool(post_id) and len(legs) == 2,
                    f"{st} id={post_id} legs={len(legs)} fee={created.get('fee_settled', {}).get('transaction', '')[:10]}"))

    # 6 replay the same payment
    st, _, replay = http("POST", f"{base}/api/posts", {"title": "edge", "body": body_text + "x", "content_hash": content_hash + "x", "window_seconds": 3600},
                         {"PAYMENT-SIGNATURE": hdr})
    results.append(("6 replayed payment rejected", st == 402, f"{st} err={replay.get('error')}"))

    # 7 paid read (REST)
    st, _, unpaid_read = http("GET", f"{base}/api/posts/{post_id}")
    acc2 = (unpaid_read.get("accepts") or [{}])[0]
    hdr2, _ = pay_header(a, payer_sk, acc2)
    st, _, read = http("GET", f"{base}/api/posts/{post_id}", None, {"PAYMENT-SIGNATURE": hdr2})
    verified = (read.get("post") or {}).get("content_hash_verified")
    results.append(("7 GET /api/posts/:id paid -> body + hash check", st == 200 and verified is True,
                    f"{st} verified={verified} body={'edge' in str(read.get('post', {}).get('body', ''))}"))

    # 8 paid read through MCP (same resource shape)
    st, mp = mcp.tool("post_read", {"post_id": post_id})
    pay_req = mp.get("result", {}).get("structuredContent", {})
    acc3 = (pay_req.get("accepts") or [{}])[0]
    hdr3, _ = pay_header(a, payer_sk, acc3)
    st, mp2 = mcp.tool("post_read", {"post_id": post_id}, payment=hdr3)
    sc = mp2.get("result", {}).get("structuredContent", {})
    results.append(("8 MCP post_read paid", mp2.get("result", {}).get("isError") is not True and bool(sc.get("receipt")),
                    f"txid={str(sc.get('receipt', {}).get('transaction'))[:10]}"))

    # 9 duplicate content hash is an objective violation
    st, _, dup_req = http("POST", f"{base}/api/posts", {"title": "edge", "body": body_text, "content_hash": content_hash, "window_seconds": 3600})
    acc4 = (dup_req.get("accepts") or [{}])[0]
    hdr4, _ = pay_header(a, payer_sk, acc4)
    st, _, dup = http("POST", f"{base}/api/posts", {"title": "edge", "body": body_text, "content_hash": content_hash, "window_seconds": 3600},
                      {"PAYMENT-SIGNATURE": hdr4})
    results.append(("9 duplicate content_hash -> 409", st == 409, f"{st} {dup.get('error')}"))

    # 10 tampered receiver
    st_unpaid, _, req10 = http("POST", f"{base}/api/posts", {"title": "t", "body": "tamper " + stamp, "content_hash": "sha256:" + stamp, "window_seconds": 3600})
    acc5 = dict((req10.get("accepts") or [{}])[0])
    hdr5, _ = pay_header(a, payer_sk, acc5)
    env = json.loads(base64.b64decode(hdr5))
    signed = encoding.msgpack_decode(env["payload"]["txn"])
    signed.transaction.receiver = account.generate_account()[1]
    env["payload"]["txn"] = enc_txn(signed)
    hdr5 = base64.b64encode(json.dumps(env).encode()).decode()
    st, _, tampered = http("POST", f"{base}/api/posts", {"title": "t", "body": "tamper " + stamp, "content_hash": "sha256:" + stamp, "window_seconds": 3600},
                           {"PAYMENT-SIGNATURE": hdr5})
    results.append(("10 tampered receiver rejected", st == 402, f"{st} err={tampered.get('error')}"))

    # 11 challenge plan
    st, _, plan = http("POST", f"{base}/api/posts/{post_id}/challenge", {})
    results.append(("11 challenge plan", st == 200 and len(plan.get("atomic_group") or []) == 2,
                    f"{st} bond={plan.get('challenger_bond_micro_algo')}"))

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
