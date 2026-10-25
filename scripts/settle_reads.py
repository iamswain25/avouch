"""Settle paid-read revenue to the answer that served it.

Reads cost 1,000 uALGO at the edge and never touch the chain one by one. The edge records each
read in D1 (``reads`` table); this job batches the maintainer share and calls the contract's
``reward`` once per slot, which raises that answer's accumulator. Backers pull their share with
``claim`` and their principal is untouched.

Cadence (also stated in the whitepaper): a slot's pending pool settles when it reaches
``--min-pool`` (default 20,000 uALGO = 40 reads), otherwise once a day, and never later than
seven days. If the slot's answer changed between the read and the settlement, that revenue is
redirected to the treasury (marked ``redirected:<ts>``) instead of being credited to the wrong
maintainers.

The job needs the read-revenue wallet key, because it pays the reward and the two transaction
fees (2,000 uALGO per batch, charged to the treasury share, never to the maintainers). That is the
trust assumption: the operator holds read revenue and signs settlements. Every settlement is a
public transaction and ``/api/meta`` exposes the unsettled balance, so the assumption is auditable.

    # local: wrangler d1 --local + localnet algod
    .venv/bin/python scripts/settle_reads.py --local --app-id <id> --dry-run

    # testnet: remote D1 + Nodely algod, key from ~/.config/avouch/testnet-app.json
    .venv/bin/python scripts/settle_reads.py --app-id <id> --network testnet
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from algosdk import abi, account, mnemonic, transaction
from algosdk.v2client import algod

DB = "avouch-board"
SECONDS_PER_DAY = 86_400


def wrangler(sql: str, local: bool) -> list[dict]:
    cmd = ["npx", "--yes", "wrangler@latest", "d1", "execute", DB, "--json"]
    cmd.append("--local" if local else "--remote")
    cmd += ["--command", sql]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise SystemExit(f"wrangler failed: {out.stderr[-400:] or out.stdout[-400:]}")
    payload = json.loads(out.stdout[out.stdout.index("["):])
    return payload[0].get("results", [])


# ARC-4 selectors are the first four bytes of SHA-512/256. This python build has no sha512/256,
# so the values are taken from the compiled artifact and the contract e2e checks them.
SELECTORS = {
    "reward(pay,uint64)void": "89b187bc",
    "claim(uint64)void": "3d1d2f0f",
}


def selector_for(signature: str) -> bytes:
    if signature not in SELECTORS:
        raise SystemExit(f"no selector for {signature}")
    return bytes.fromhex(SELECTORS[signature])


def load_key(network: str) -> str:
    home = Path.home() / ".config" / "avouch"
    if network == "testnet":
        return mnemonic.to_private_key(json.loads((home / "testnet-app.json").read_text())["treasury_mnemonic"])
    # localnet: the sandbox dispenser key
    from algosdk.kmd import KMDClient
    k = KMDClient("a" * 64, "http://localhost:4002")
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    return k.export_key(h, "", k.list_keys(h)[0])


def algod_client(network: str) -> algod.AlgodClient:
    if network == "testnet":
        return algod.AlgodClient("", "https://testnet-api.algonode.cloud")
    return algod.AlgodClient("a" * 64, "http://localhost:4001")


def slot_key(sid: int) -> bytes:
    return b"s" + sid.to_bytes(8, "big")


def decode_slot(v: bytes) -> dict:
    def u64(o: int) -> int:
        return int.from_bytes(v[o : o + 8], "big")

    def tail(o: int) -> str:
        return v[o + 2 : o + 2 + ((v[o] << 8) | v[o + 1])].hex()

    return {"top_hash": tail((v[0] << 8) | v[1]), "top_total": u64(2), "second_total": u64(12), "epoch": u64(20)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="localnet", choices=["localnet", "testnet"])
    ap.add_argument("--local", action="store_true", help="use the local D1 (wrangler --local)")
    ap.add_argument("--app-id", type=int, required=True)
    ap.add_argument("--min-pool", type=int, default=20_000, help="settle a slot pool at this amount (uALGO)")
    ap.add_argument("--daily", type=int, default=SECONDS_PER_DAY, help="settle smaller pools after this age (s)")
    ap.add_argument("--max-age", type=int, default=7 * SECONDS_PER_DAY, help="never hold a read longer than this (s)")
    ap.add_argument("--force", action="store_true", help="settle every pending pool regardless of size or age")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    local = args.local or args.network == "localnet"
    a = algod_client(args.network)
    sk = load_key(args.network)
    payer = account.address_from_private_key(sk)
    app_address = transaction.logic.get_application_address(args.app_id)
    now = int(time.time())

    groups = wrangler(
        "SELECT slot_id, content_hash, COUNT(*) AS reads, SUM(maintainer_micro_algo) AS amount, MIN(created_at) AS oldest "
        "FROM reads WHERE batch_id IS NULL GROUP BY slot_id, content_hash ORDER BY oldest",
        local,
    )
    plan, redirected = [], []
    for g in groups:
        amount = int(g["amount"] or 0)
        age = now - int(g["oldest"] or now)
        big = amount >= args.min_pool
        daily = age >= args.daily and amount >= 5_000
        overdue = age >= args.max_age and amount > 0
        if not (args.force or big or daily or overdue):
            continue
        sid = int(g["slot_id"])
        try:
            box = a.application_box_by_name(args.app_id, slot_key(sid))
            state = decode_slot(base64.b64decode(box["value"]))
        except Exception:
            redirected.append({**g, "reason": "slot box unreadable"})
            continue
        current = "sha256:" + state["top_hash"]
        if state["top_total"] == 0 or current != g["content_hash"]:
            redirected.append({**g, "reason": "answer changed since the read"})
            continue
        plan.append({**g, "amount": amount, "sid": sid, "hash": state["top_hash"]})

    print(f"pending groups: {len(groups)} | settle now: {len(plan)} | redirect to treasury: {len(redirected)}")
    for p in plan:
        print(f"  slot {p['slot_id']} reads={p['reads']} amount={p['amount']} age={now - int(p['oldest'])}s")
    for r in redirected:
        print(f"  redirect slot {r['slot_id']} reads={r['reads']} amount={r['amount']}: {r['reason']}")
    if args.dry_run:
        return 0

    sel = selector_for("reward(pay,uint64)void")
    done = []
    for p in plan:
        sp = a.suggested_params()
        sp.flat_fee = True
        sp.fee = 1000
        pay = transaction.PaymentTxn(payer, sp, app_address, p["amount"])
        sp2 = a.suggested_params()
        sp2.flat_fee = True
        sp2.fee = 1000
        call = transaction.ApplicationNoOpTxn(
            payer, sp2, args.app_id,
            app_args=[sel, p["sid"].to_bytes(8, "big")],
            boxes=[(args.app_id, slot_key(p["sid"])), (args.app_id, b"a" + p["sid"].to_bytes(8, "big") + bytes.fromhex(p["hash"]))],
        )
        transaction.assign_group_id([pay, call])
        try:
            txid = a.send_transactions([pay.sign(sk), call.sign(sk)])
            transaction.wait_for_confirmation(a, txid, 10)
        except Exception as err:
            print(f"  settlement failed for slot {p['slot_id']}: {err}")
            continue
        done.append({"slot": p["slot_id"], "txid": txid, "amount": p["amount"], "reads": p["reads"]})
        ids = [str(r["id"]) for r in wrangler(
            f"SELECT id FROM reads WHERE batch_id IS NULL AND slot_id = '{p['slot_id']}' AND content_hash = '{p['content_hash']}'", local)]
        if ids:
            wrangler(f"UPDATE reads SET batch_id = '{txid}' WHERE id IN ({','.join(ids)})", local)
        print(f"  settled slot {p['slot_id']}: {txid} ({p['amount']} uALGO, {p['reads']} reads)")

    for r in redirected:
        wrangler(f"UPDATE reads SET batch_id = 'redirected:{now}' WHERE batch_id IS NULL AND slot_id = '{r['slot_id']}' AND content_hash = '{r['content_hash']}'", local)

    if done:
        wrangler(
            "INSERT INTO settlements (batch_id, created_at, groups, amount_micro_algo, txids) VALUES "
            f"('batch-{now}', {now}, {len(done)}, {sum(d['amount'] for d in done)}, '{','.join(d['txid'] for d in done)}')",
            local,
        )
    print(json.dumps({"settled": done, "redirected": len(redirected)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
