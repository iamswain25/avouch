"""Refund unspent prepaid read credit.

The deposit sits in the operator's pay-to account, so a refund is a payment back from that
account. Run this on a schedule (or before expiry) so the promise in the docs and the
whitepaper - "unspent balance is refunded by the operator" - is a command, not a claim.

    # show what is refundable
    .venv/bin/python scripts/refund_credit.py --local --dry-run
    # pay refunds for credits that are expired or empty-idle past --idle-days
    .venv/bin/python scripts/refund_credit.py --network testnet --idle-days 30

A credit is refundable when balance > 0 and either it is expired or it has been idle for
--idle-days. Every refund is recorded in `refunded_txid`, so a second run cannot pay twice.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
import time
from pathlib import Path

from algosdk import account, mnemonic, transaction
from algosdk.v2client import algod

DB = "avouch-board"

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _payload(stdout: str) -> list:
    """wrangler prints warnings and banner text before the JSON; take the last JSON array."""
    text = ANSI.sub("", stdout)
    m = re.search(r"\[\s*\{\s*\"results\".*\}\s*\]", text, re.S)
    if not m:
        raise SystemExit(f"could not find the d1 json payload in: {text[:300]}")
    return json.loads(m.group(0))



def wrangler(sql: str, local: bool) -> list[dict]:
    cmd = ["npx", "--yes", "wrangler@latest", "d1", "execute", DB, "--json", "--local" if local else "--remote", "--command", sql]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise SystemExit(f"wrangler failed: {out.stderr[-400:] or out.stdout[-400:]}")
    return _payload(out.stdout)[0].get("results", [])


def txn_sender(txid: str, network: str) -> str:
    """The payer of a deposit, read from the chain when the edge did not record it."""
    if network == "testnet":
        base, idx = "https://testnet-api.algonode.cloud", "https://testnet-idx.algonode.cloud"
    else:
        base, idx = "http://localhost:4001", "http://localhost:8980"
    try:
        with urllib.request.urlopen(f"{idx}/v2/transactions/{txid}", timeout=30) as r:
            return json.loads(r.read().decode())["transaction"]["sender"]
    except Exception:
        with urllib.request.urlopen(f"{base}/v2/transactions/pending/{txid}", timeout=30) as r:
            return json.loads(r.read().decode()).get("sender", "")


def load_key(network: str) -> str:
    home = Path.home() / ".config" / "avouch"
    if network == "testnet":
        return mnemonic.to_private_key(json.loads((home / "testnet-app.json").read_text())["treasury_mnemonic"])
    from algosdk.kmd import KMDClient

    k = KMDClient("a" * 64, "http://localhost:4002")
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    return k.export_key(h, "", k.list_keys(h)[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="localnet", choices=["localnet", "testnet"])
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--idle-days", type=int, default=30, help="refund credits idle this long even before expiry")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    local = args.local or args.network == "localnet"
    a = algod.AlgodClient("a" * 64, "http://localhost:4001") if local else algod.AlgodClient("", "https://testnet-api.algonode.cloud")
    sk = load_key(args.network)
    payer = account.address_from_private_key(sk)
    now = int(time.time())
    idle_cut = now - args.idle_days * 86_400

    rows = wrangler(
        "SELECT credit_id, payer, balance_micro_algo, updated_at, expires_at, deposit_txid "
        "FROM credits WHERE balance_micro_algo > 0 AND deposit_txid IS NOT NULL AND refunded_txid IS NULL "
        f"AND (expires_at <= {now} OR updated_at <= {idle_cut}) ORDER BY updated_at",
        local,
    )
    print(f"refundable credits: {len(rows)} | operator {payer} {a.account_info(payer)['amount'] / 1e6:.3f} ALGO")
    if not rows:
        return 0

    paid = 0
    for r in rows:
        target = r["payer"] or txn_sender(r["deposit_txid"], args.network)
        amount = int(r["balance_micro_algo"])
        if not target or amount <= 0:
            print(f"  {r['credit_id']}: no payer to refund")
            continue
        print(f"  {r['credit_id']} -> {target[:8]}… {amount} uALGO")
        if args.dry_run:
            continue
        sp = a.suggested_params()
        sp.flat_fee = True
        sp.fee = 1000
        txn = transaction.PaymentTxn(payer, sp, target, amount, note=f"refund:{r['credit_id']}".encode())
        try:
            txid = a.send_transaction(txn.sign(sk))
            transaction.wait_for_confirmation(a, txid, 20)
        except Exception as err:
            print(f"    refund failed: {err}")
            continue
        wrangler(
            f"UPDATE credits SET balance_micro_algo = 0, payer = '{target}', refunded_txid = '{txid}', updated_at = {now} "
            f"WHERE credit_id = '{r['credit_id']}' AND refunded_txid IS NULL",
            local,
        )
        paid += amount
        print(f"    refunded {txid}")

    print(json.dumps({"refunded_micro_algo": paid, "credits": len(rows)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
