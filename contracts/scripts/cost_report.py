"""Measure the real per-post cost of the escrow on localnet.

Reports, for one full post -> challenge -> slash cycle:
  - ALGO fees paid by each participant (from confirmed txns)
  - box MBR the app has to carry while a post is open (locked, not burned)
  - USD equivalent at the ALGO price given on the command line

Run: .venv/bin/python scripts/cost_report.py [algo_usd]
"""

from __future__ import annotations

import sys

import e2e_localnet as E


def fees_of(a, txids: list[str]) -> int:
    return sum(a.pending_transaction_info(t)["txn"]["txn"]["fee"] for t in txids)


def main() -> int:
    algo_usd = float(sys.argv[1]) if len(sys.argv) > 1 else 0.104714
    a, disp = E.connect()
    asset = E.create_asset(a, disp)

    author, challenger, arbiter = E.Acct.generate(), E.Acct.generate(), E.Acct.generate()
    for acct in (author, challenger, arbiter):
        E.send_pay(a, disp, acct.address, 5_000_000)
        sp = a.suggested_params()
        t = E.transaction.AssetTransferTxn(acct.address, sp, acct.address, 0, asset)
        E.wait(a, a.send_transaction(t.sign(acct.private_key)))
        E.send_asset(a, disp, acct.address, 100 * E.ONE, asset)

    app = E.deploy(a, disp, [asset, arbiter.address, disp.address, E.ONE, 10000, 5000, 4])
    addr = E.app_addr(app)
    E.send_pay(a, disp, addr, 3_000_000)
    E.call(a, disp, app, "opt_in_usdc()void", [], assets=[asset], fee=2000)

    everyone = [author.address, challenger.address, arbiter.address, disp.address]
    baseline_mbr = a.account_info(addr)["min-balance"]

    # post (2 txns), challenge (2 txns), settle (1 outer + 2 inner)
    E.group_post(a, author, app, asset, 42, 60, accounts=everyone)
    after_post_mbr = a.account_info(addr)["min-balance"]
    E.group_challenge(a, challenger, app, asset, 42, accounts=everyone)
    E.call(a, arbiter, app, "settle(uint64,bool)void", [42, True],
           boxes=[E.box_key(42)], accounts=everyone, assets=[asset])
    after_settle_mbr = a.account_info(addr)["min-balance"]

    mbr_locked = after_post_mbr - baseline_mbr
    settle_fee = 3000
    post_fees = 2000
    challenge_fees = 2000
    cycle_fees = post_fees + challenge_fees + settle_fee

    def usd(micro_algo: int) -> float:
        return micro_algo / 1e6 * algo_usd

    print(f"ALGO price used: ${algo_usd}")
    print()
    print("one post -> challenge -> slash cycle")
    print(f"  post      : 2 txns, fee {post_fees:,} uALGO  (${usd(post_fees):.6f})")
    print(f"  challenge : 2 txns, fee {challenge_fees:,} uALGO  (${usd(challenge_fees):.6f})")
    print(f"  settle    : 1 outer + 2 inner, fee {settle_fee:,} uALGO  (${usd(settle_fee):.6f})")
    print(f"  total fees: {cycle_fees:,} uALGO = {cycle_fees/1e6:.6f} ALGO = ${usd(cycle_fees):.6f}")
    print()
    print(f"box MBR locked while the post is open: {mbr_locked:,} uALGO"
          f" ({mbr_locked/1e6:.4f} ALGO, ${usd(mbr_locked):.4f})")
    print(f"  app min-balance {baseline_mbr:,} -> after post {after_post_mbr:,}"
          f" -> after settle {after_settle_mbr:,}")
    print("  (MBR returns to the app account when the box is deleted; it is float, not a burn)")
    print()
    print("so the per-post on-chain cost is fees + MBR float:")
    print(f"  fees only : ${usd(cycle_fees):.6f}")
    print(f"  with MBR  : ${usd(cycle_fees + mbr_locked):.6f}  <-- what the platform must hold per open post")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
