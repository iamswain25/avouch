"""Smoke test for prepaid read credit against a running edge.

    python scripts/credit_smoke.py --base http://127.0.0.1:8789 --reads 10 --slot 4242

It tops up a credit, pays it on chain, redeems it, reads until the balance runs out, and
reports the maintainer pool that the settlement job will pick up.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request

from algosdk import account, encoding, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

UA = {"User-Agent": "avouch-credit-smoke/1.0", "Content-Type": "application/json"}


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def localnet_key(network):
    if network == "testnet":
        raise SystemExit("pass --key-file for testnet; this smoke test funds itself from localnet KMD")
    k = KMDClient("a" * 64, "http://localhost:4002")
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return sk, account.address_from_private_key(sk)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8789")
    ap.add_argument("--reads", type=int, default=10)
    ap.add_argument("--slot", default="4242")
    ap.add_argument("--network", default="localnet", choices=["localnet", "testnet"])
    args = ap.parse_args()

    a = algod_client(args.network)
    sk, addr = localnet_key(args.network)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))
        print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""))

    # 1. topup
    st, top = http("POST", f"{args.base}/api/credit/topup", {"reads": args.reads})
    check("1 topup returns a payment plan", st == 201 and "payment" in top, f"{st} {top.get('amount_micro_algo')} uALGO")
    cid = top["credit_id"]
    pay_instr = top["payment"]

    # 2. pay it
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    pay = transaction.PaymentTxn(addr, sp, pay_instr["receiver"], int(pay_instr["amount_micro_algo"]), note=pay_instr["note"].encode())
    signed = pay.sign(sk)
    env = {"x402Version": 2, "scheme": "exact", "network": pay_instr["network"], "payload": {"txn": encoding.msgpack_encode(signed)}}
    header = base64.b64encode(json.dumps(env).encode()).decode()

    # 3. redeem
    st, red = http("POST", f"{args.base}/api/credit/redeem/{cid}", None, {"PAYMENT-SIGNATURE": header})
    token = red.get("credit_token", "")
    check("2 redeem yields a credit token", st == 200 and bool(token), f"{st} balance={red.get('balance_micro_algo')} reads={red.get('reads_remaining')}")
    st, again = http("POST", f"{args.base}/api/credit/redeem/{cid}", None, {"PAYMENT-SIGNATURE": header})
    check("3 a deposit cannot be redeemed twice", st == 409, f"{st} {again.get('error','')}")

    # 4. reads debit the balance
    balances = []
    for i in range(args.reads):
        st, out = http("GET", f"{args.base}/api/posts/{args.slot}", None, {"X-CREDIT": token})
        if st != 200:
            check(f"4 read {i+1} failed", False, f"{st} {json.dumps(out)[:120]}")
            break
        balances.append(out["receipt"]["balance_micro_algo"])
    check("4 reads are served from credit and debit it", len(balances) == args.reads and balances == sorted(balances, reverse=True),
          f"first={balances[0] if balances else None} last={balances[-1] if balances else None}")

    st, out = http("GET", f"{args.base}/api/posts/{args.slot}", None, {"X-CREDIT": token})
    check("5 an exhausted credit returns 402", st == 402, f"{st} {out.get('error','')}")

    st, info = http("GET", f"{args.base}/api/credit/{cid}", None, {"X-CREDIT": token})
    check("6 balance endpoint agrees", st == 200 and info.get("reads_remaining") == 0 and info.get("reads") == args.reads,
          f"{st} reads={info.get('reads')} left={info.get('reads_remaining')}")

    st, bad = http("GET", f"{args.base}/api/credit/{cid}", None, {"X-CREDIT": cid + ".deadbeef"})
    check("7 a forged credit token is refused", st == 403, f"{st}")

    st, meta = http("GET", f"{args.base}/api/meta")
    pending = meta["read_revenue"]["pending_micro_algo"]
    check("8 reads are queued for maintainer settlement", pending >= args.reads * 500, f"pending={pending} uALGO")

    # 9. one-off payment still works alongside credit
    st, quote = http("GET", f"{args.base}/api/posts/{args.slot}")
    ok = st == 402 and "cheaper" in quote
    check("9 the 402 still offers the one-off path and points at prepaid", ok, f"{st}")

    print()
    print("RESULT:", "ALL PASS" if all(c[1] for c in checks) else "FAILURES")
    return 0 if all(c[1] for c in checks) else 1


def algod_client(network):
    if network == "testnet":
        return algod.AlgodClient("", "https://testnet-api.algonode.cloud")
    return algod.AlgodClient("a" * 64, "http://localhost:4001")


if __name__ == "__main__":
    sys.exit(main())
