"""Seed one answer into a local board, so a fresh clone is not an empty board.

A framework that boots to zero answers has no first read to show. This stakes one answer
into a slot through the same edge plan a client would use, then prints what a reader sees:
the free index entry, the 402 body, and the paid read once the payment is made.

    algokit localnet start
    eval "$(uv run --with py-algorand-sdk python scripts/deploy_slot_localnet.py --export)"
    npx --yes wrangler@latest d1 execute avouch-board --local --file=schema.sql
    npx --yes wrangler@latest pages dev site --port 8789 --binding ...   # see README
    uv run --with py-algorand-sdk python scripts/seed_localnet.py --slot 1 \
        --title "read price" --body "1,000 microALGO per read; 500 to the maintainer, 500 to the treasury."

Run: uv run --with py-algorand-sdk python scripts/seed_localnet.py [--base URL] [--amount 1000000]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_worker_localnet import (  # noqa: E402
    connect,
    exec_plan,
    http,
    pay_header,
)

DEFAULT_BODY = (
    "A read costs 1,000 microALGO. 500 goes to the backers of the answer that served it and "
    "500 to the treasury. The price is public before you pay: the 402 body names it."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8789")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--title", default="read price")
    ap.add_argument("--body", default=DEFAULT_BODY)
    ap.add_argument("--amount", type=int, default=0, help="microALGO; 0 uses the edge policy")
    ap.add_argument("--pay-read", action="store_true", help="also pay once and print the served body")
    ap.add_argument("--network", default="localnet", choices=["localnet", "testnet"],
                    help="testnet uses ~/.config/avouch/testnet.json as the staker")
    args = ap.parse_args()

    a, sk, payer = connect(args.network)

    status, _, meta = http("GET", f"{args.base}/api/meta")
    if status != 200:
        print(f"edge is not answering: {status} {meta}", file=sys.stderr)
        return 1
    policy = int(
        meta.get("policy_first_stake_micro_algo")
        or meta.get("policy_min_stake_micro_algo")
        or meta.get("min_stake_micro_algo")
        or 0
    )
    amount = args.amount or max(policy, 100_000)
    print(f"edge {status} app={meta.get('app_id')} policy={policy} amount={amount}")

    content_hash = "sha256:" + hashlib.sha256(args.body.encode()).hexdigest()

    def ask_plan(amount_micro_algo: int):
        return http("POST", f"{args.base}/api/posts/{args.slot}/stake", {
            "sender": payer,
            "content_hash": content_hash,
            "body": args.body,
            "title": args.title,
            "amount_micro_algo": amount_micro_algo,
        })

    status, _, plan = ask_plan(amount)
    if status == 400 and not args.amount:
        # the free meta and the stake gate name the policy differently; take the gate's number
        hinted = int((plan or {}).get("policy_first_stake_micro_algo") or 0)
        if hinted and hinted != amount:
            print(f"policy from the gate: {hinted}")
            amount = hinted
            status, _, plan = ask_plan(amount)
    if status != 200:
        print(f"stake plan refused: {status} {json.dumps(plan, ensure_ascii=False)[:400]}", file=sys.stderr)
        return 1
    print(f"plan {status}: {len(plan['atomic_group'])} legs")

    txid = exec_plan(a, sk, int(meta["app_id"]), plan)
    print(f"stake submitted: {txid}")

    status, _, idx = http("GET", f"{args.base}/api/posts")
    row = next((s for s in (idx.get("slots") or []) if str(s.get("slot_id")) == str(args.slot)), None)
    if status == 200 and row is not None:
        print(f"index {status}: epoch={row.get('epoch')} stake={row.get('top_stake_micro_algo')} "
              f"hash={row.get('top_hash')} body_stored={row.get('top_body_stored')}")
    else:
        print(f"index {status}: slot {args.slot} not listed ({json.dumps(idx, ensure_ascii=False)[:300]})",
              file=sys.stderr)
        return 1

    status, _, challenge = http("GET", f"{args.base}/api/posts/{args.slot}")
    accept = (challenge.get("accepts") or [{}])[0]
    print(f"\nreader sees {status} (payment required), {accept.get('maxAmountRequired')} uALGO "
          f"to {accept.get('payTo')}")

    if args.pay_read:
        header, _ = pay_header(a, sk, accept)
        status, _, served = http("GET", f"{args.base}/api/posts/{args.slot}",
                                 headers={"PAYMENT-SIGNATURE": header})
        answer = served.get("answer") or {}
        receipt = served.get("receipt") or {}
        print(f"paid read {status}: verified={answer.get('content_hash_verified')} "
              f"body={str(answer.get('body'))[:60]!r} paid={receipt.get('paid_micro_algo')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
