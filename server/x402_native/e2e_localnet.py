"""End-to-end test of the native-ALGO x402 flow against algokit localnet.

Cases
  1 free           : /free answers 200 without payment
  2 challenge      : /paid answers 402 with accepts[0].asset == "ALGO"
  3 settle         : client pays, retries, gets 200 + PAYMENT-RESPONSE, txid on chain
  4 replay         : the same header twice -> second attempt rejected
  5 underpay       : amount below maxAmountRequired -> rejected, no settlement
  6 wrong resource : challenge issued for /paid reused on another resource -> rejected
  7 tamper         : receiver swapped after signing -> rejected

Run: .venv/bin/python server/x402_native/e2e_localnet.py
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

import algosdk
from algosdk import account, encoding, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

from demo_server import PRICE_MICRO_ALGO, serve  # noqa: E402
from verifier import MIN_FEE, NativeX402  # noqa: E402

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
PORT = 8851


def connect():
    a = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return a, sk, account.address_from_private_key(sk)


def fund(a, dispenser_sk, to, amount):
    sp = a.suggested_params()
    txn = transaction.PaymentTxn(account.address_from_private_key(dispenser_sk), sp, to, amount)
    transaction.wait_for_confirmation(a, a.send_transaction(txn.sign(dispenser_sk)), 10)


def http(method, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, dict(e.headers), parsed


def pay_header(a, payer_sk, pay_to, amount, payment_id_hex):
    signed = NativeX402.build_payment_txn(payer_sk, a, pay_to, amount, payment_id_hex)
    return NativeX402.build_payment_header(signed), signed


def main() -> int:
    a, dispenser_sk, dispenser = connect()
    print("dispenser:", dispenser[:12], "round:", a.status()["last-round"])

    payer_sk, payer = account.generate_account()
    fund(a, dispenser_sk, payer, 5_000_000)

    server = serve(dispenser, PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)

    results: list[tuple[str, bool, str]] = []
    try:
        # 1 free
        code, _, body = http("GET", "/free")
        results.append(("1 free: 200 without payment", code == 200 and body.get("ok") is True, f"{code}"))

        # 2 challenge
        code, _, body = http("GET", "/paid")
        accepts = (body.get("accepts") or [{}])[0]
        pid = (accepts.get("extra") or {}).get("paymentId", "")
        ch_ok = code == 402 and accepts.get("asset") == "ALGO" and accepts.get("scheme") == "exact" and bool(pid)
        results.append(("2 challenge: 402 with asset ALGO", ch_ok,
                        f"{code} asset={accepts.get('asset')} amount={accepts.get('maxAmountRequired')}"))

        # 3 settle
        bal_before = a.account_info(dispenser)["amount"]
        header, signed = pay_header(a, payer_sk, dispenser, PRICE_MICRO_ALGO, pid)
        code, headers, body = http("GET", "/paid", {"PAYMENT-SIGNATURE": header})
        receipt_hdr = headers.get("PAYMENT-RESPONSE") or headers.get("Payment-Response")
        receipt = json.loads(base64.b64decode(receipt_hdr)) if receipt_hdr else {}
        bal_after = a.account_info(dispenser)["amount"]
        paid_ok = (code == 200 and receipt.get("success") is True and
                   receipt.get("transaction") == signed.transaction.get_txid() and
                   bal_after - bal_before == PRICE_MICRO_ALGO)
        results.append(("3 settle: 200 + PAYMENT-RESPONSE, balance moved", paid_ok,
                        f"{code} txid={str(receipt.get('transaction'))[:12]} delta={bal_after - bal_before}"))

        # 4 replay
        code2, _, body2 = http("GET", "/paid", {"PAYMENT-SIGNATURE": header})
        replay_ok = code2 == 402
        results.append(("4 replay: same header rejected", replay_ok, f"{code2} err={body2.get('error')}"))

        # 5 underpay
        code, _, body = http("GET", "/paid")
        pid5 = (body["accepts"][0]["extra"])["paymentId"]
        header5, _ = pay_header(a, payer_sk, dispenser, PRICE_MICRO_ALGO - 1, pid5)
        code5, _, body5 = http("GET", "/paid", {"PAYMENT-SIGNATURE": header5})
        results.append(("5 underpay: rejected", code5 == 402, f"{code5} err={body5.get('error')}"))

        # 6 wrong resource: reuse a /paid challenge id on another resource (server-side check)
        x402 = server.x402  # type: ignore[attr-defined]
        ch = x402.new_challenge("/other", PRICE_MICRO_ALGO)
        header6, _ = pay_header(a, payer_sk, dispenser, PRICE_MICRO_ALGO, ch.payment_id.hex())
        try:
            x402.verify_and_settle(header6, "/paid")
            wrong_ok, detail = False, "accepted"
        except ValueError as exc:
            wrong_ok, detail = True, str(exc)
        results.append(("6 wrong resource: rejected", wrong_ok, detail))

        # 7 tamper: sign for the payer, then rewrite the receiver
        code, _, body = http("GET", "/paid")
        pid7 = (body["accepts"][0]["extra"])["paymentId"]
        signed7 = NativeX402.build_payment_txn(payer_sk, a, dispenser, PRICE_MICRO_ALGO, pid7)
        attacker = account.generate_account()[1]
        signed7.transaction.receiver = attacker
        tampered = base64.b64encode(json.dumps({
            "x402Version": 2, "scheme": "exact",
            "network": os.environ.get("X402_NETWORK", "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe"),
            "payload": {"txn": encoding.msgpack_encode(signed7)},
        }).encode()).decode()
        code7, _, body7 = http("GET", "/paid", {"PAYMENT-SIGNATURE": tampered})
        results.append(("7 tamper: receiver rewrite rejected", code7 == 402, f"{code7} err={body7.get('error')}"))
    finally:
        server.shutdown()

    print()
    allok = True
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name, "|", detail)
        allok &= bool(ok)
    print()
    print("RESULT:", "ALL PASS" if allok else "FAILURES PRESENT")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
