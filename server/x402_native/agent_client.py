"""A wallet-only agent: pays a native-ALGO 402 without any x402 SDK.

The point of this file is to test the claim "an agent can handle a non-standard 402
by itself". It reads the 402 body, picks the ALGO accept, builds the payment from
the fields it was given, and retries. No scheme library, no registration, no
hardcoded price or address.

Run: python server/x402_native/agent_client.py        # starts a local server + localnet payer
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

from algosdk import account, encoding, transaction  # noqa: E402
from algosdk.kmd import KMDClient  # noqa: E402
from algosdk.v2client import algod  # noqa: E402

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64


def enc_txn(signed) -> str:
    """payload.txn must be base64 of the msgpack bytes (normalized across SDK versions)."""
    v = encoding.msgpack_encode(signed)
    try:
        if base64.b64decode(v, validate=True)[:1] in (b"\x81", b"\x82", b"\x83", b"\x84"):
            return v
    except Exception:
        pass
    return base64.b64encode(v.encode("latin-1")).decode()


def http_get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read().decode())


def pay(url: str, private_key: str, client: algod.AlgodClient, prefer: str = "ALGO") -> dict:
    """One agent turn: GET -> read 402 -> pay -> GET again -> receipt."""
    status, _, body = http_get(url)
    if status != 402:
        return {"paid": False, "status": status, "reason": "resource did not ask for payment"}

    accepts = body.get("accepts") or []
    choice = next((a for a in accepts if a.get("asset") == prefer), None) or (accepts[0] if accepts else None)
    if choice is None:
        return {"paid": False, "status": status, "reason": "no acceptable payment option"}

    scheme, asset = choice.get("scheme"), choice.get("asset")
    if scheme != "exact" or asset != "ALGO":
        # this client only knows the native-ALGO shape; anything else is out of scope
        return {"paid": False, "status": status, "reason": f"unsupported scheme/asset {scheme}/{asset}"}

    amount = int(choice["maxAmountRequired"])
    pay_to = choice["payTo"]
    payment_id = (choice.get("extra") or {}).get("paymentId", "")
    sender = account.address_from_private_key(private_key)

    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.PaymentTxn(sender, sp, pay_to, amount, note=payment_id.encode())
    signed = txn.sign(private_key)
    envelope = {
        "x402Version": body.get("x402Version", 2),
        "scheme": scheme,
        "network": choice["network"],
        "payload": {"txn": enc_txn(signed)},
    }
    header = base64.b64encode(json.dumps(envelope).encode()).decode()

    status2, headers2, body2 = http_get(url, {"PAYMENT-SIGNATURE": header})
    receipt_hdr = headers2.get("PAYMENT-RESPONSE") or headers2.get("Payment-Response")
    receipt = json.loads(base64.b64decode(receipt_hdr)) if receipt_hdr else {}
    return {"paid": status2 == 200, "status": status2, "receipt": receipt, "body": body2}


def _dispenser():
    c = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return c, sk, account.address_from_private_key(sk)


def main() -> int:
    from demo_server import serve

    client, dispenser_sk, dispenser = _dispenser()
    payer_sk, payer = account.generate_account()
    sp = client.suggested_params()
    fund = transaction.PaymentTxn(dispenser, sp, payer, 5_000_000)
    transaction.wait_for_confirmation(client, client.send_transaction(fund.sign(dispenser_sk)), 10)

    server = serve(dispenser, 8852)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    try:
        before = client.account_info(dispenser)["amount"]
        out = pay("http://127.0.0.1:8852/paid", payer_sk, client)
        after = client.account_info(dispenser)["amount"]
    finally:
        server.shutdown()

    print(json.dumps(out, indent=1))
    ok = out.get("paid") is True and (after - before) == 1000
    print()
    print("payer wallet held ALGO only. no ASA opt-in, no x402 SDK, no hardcoded price.")
    print("recipient delta:", after - before)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
