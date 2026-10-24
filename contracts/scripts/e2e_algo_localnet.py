"""End-to-end localnet run of the native-ALGO bond escrow (PostBondAlgo).

Same five scenarios as the USDC version, plus an escrow-solvency check that only
matters for a native bond (bond and box MBR share one balance).

  A slash    : post -> challenge -> arbiter slashes -> treasury + challenger paid
  B release  : post -> challenge -> arbiter releases -> both refunded
  C reclaim  : post -> window expires -> author reclaims unilaterally
  D stale    : post -> challenge -> arbiter stalls -> anyone refunds both
  E guards   : early reclaim rejected, non-arbiter settle rejected

Run: .venv/bin/python scripts/e2e_algo_localnet.py
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import algosdk
from algosdk import abi, account, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
ART = Path(__file__).resolve().parent.parent / "src" / "artifacts"

ONE = 1_000_000
BOND = 1 * ONE            # author bond, microALGO
CHAL = 1 * ONE            # challenger bond (1:1)
SLASH_SHARE_BPS = 5000
RESOLVE_WINDOW = 4
FEE = 1000


class Acct:
    def __init__(self, sk: str, addr: str | None = None):
        self.private_key = sk
        self.address = addr or account.address_from_private_key(sk)

    @classmethod
    def generate(cls) -> "Acct":
        sk, addr = account.generate_account()
        return cls(sk, addr)


def connect():
    a = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return a, Acct(sk)


def wait(a, txn_id):
    transaction.wait_for_confirmation(a, txn_id, 10)


def send_pay(a, sender, receiver, amount):
    sp = a.suggested_params()
    txn = transaction.PaymentTxn(sender.address, sp, receiver, amount)
    wait(a, a.send_transaction(txn.sign(sender.private_key)))


def app_addr(app_id: int) -> str:
    return transaction.logic.get_application_address(app_id)


def bal(a, addr) -> int:
    return a.account_info(addr)["amount"]


def box_key(post_id: int) -> bytes:
    return b"p" + post_id.to_bytes(8, "big")


def deploy(a, creator, args: list) -> int:
    src = json.loads((ART / "PostBondAlgo.arc56.json").read_text())
    approval = base64.b64decode(src["byteCode"]["approval"])
    clear = base64.b64decode(src["byteCode"]["clear"])
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    method = abi.Method.from_signature("create(address,address,uint64,uint64,uint64,uint64)void")
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(args)]
    txn = transaction.ApplicationCreateTxn(
        creator.address, sp, transaction.OnComplete.NoOpOC, approval, clear,
        transaction.StateSchema(5, 2), transaction.StateSchema(0, 0), app_args=app_args,
    )
    wait(a, a.send_transaction(txn.sign(creator.private_key)))
    return a.pending_transaction_info(txn.get_txid())["application-index"]


def call(a, sender, app_id, sig: str, values: list, *, boxes=(), accounts=(), fee=3000):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    # raw keys need the app id; newer py-algorand-sdk refuses to guess it
    box_refs = [(app_id, b) if isinstance(b, (bytes, bytearray)) else b for b in boxes]
    txn = transaction.ApplicationNoOpTxn(
        sender.address, sp, app_id, app_args=app_args,
        accounts=list(accounts), boxes=box_refs,
    )
    wait(a, a.send_transaction(txn.sign(sender.private_key)))
    return txn.get_txid()


def group_post(a, author, app_id, post_id, window, bond=BOND, accounts=()):
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    pay = transaction.PaymentTxn(author.address, sp, app_addr(app_id), bond)
    method = abi.Method.from_signature("post_bond(pay,uint64,byte[],uint64)void")
    content_hash = f"sha256:algo-post-{post_id}-bond-aware-cafe".encode()
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 3000
    appcall = transaction.ApplicationNoOpTxn(
        author.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(post_id),
                  method.args[2].type.encode(content_hash), method.args[3].type.encode(window)],
        accounts=list(accounts), boxes=[(app_id, box_key(post_id))],
    )
    transaction.assign_group_id([pay, appcall])
    wait(a, a.send_transactions([pay.sign(author.private_key), appcall.sign(author.private_key)]))


def group_challenge(a, challenger, app_id, post_id, amount=CHAL, accounts=()):
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    pay = transaction.PaymentTxn(challenger.address, sp, app_addr(app_id), amount)
    method = abi.Method.from_signature("challenge(pay,uint64)void")
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 3000
    appcall = transaction.ApplicationNoOpTxn(
        challenger.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(post_id)],
        accounts=list(accounts), boxes=[(app_id, box_key(post_id))],
    )
    transaction.assign_group_id([pay, appcall])
    wait(a, a.send_transactions([pay.sign(challenger.private_key), appcall.sign(challenger.private_key)]))


def poke(a, acct):
    sp = a.suggested_params()
    txn = transaction.PaymentTxn(acct.address, sp, acct.address, 0)
    wait(a, a.send_transaction(txn.sign(acct.private_key)))


def ledger_now(a) -> int:
    return a.block_info(a.status()["last-round"])["block"]["ts"]


def wait_ledger(a, ts: int, poker=None, timeout_s: float = 60.0) -> int:
    """Wait until the ledger clock passes ts; devmode timestamps can outrun the wall clock."""
    start = time.time()
    while ledger_now(a) < ts + 1:
        if time.time() - start > timeout_s:
            raise TimeoutError(f"ledger clock stuck at {ledger_now(a)} < {ts + 1}")
        if poker is not None:
            poke(a, poker)
        time.sleep(0.2)
    return ledger_now(a)


def post_state(a, app_id, post_id, accounts):
    method = abi.Method.from_signature(
        "get_post(uint64)(address,address,byte[],uint64,uint64,uint64,uint64,uint8)")
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    txn = transaction.ApplicationNoOpTxn(
        accounts[0].address, sp, app_id,
        app_args=[method.get_selector(), method.args[0].type.encode(post_id)],
        accounts=list(x.address for x in accounts), boxes=[(app_id, box_key(post_id))],
    )
    wait(a, a.send_transaction(txn.sign(accounts[0].private_key)))
    res = a.pending_transaction_info(txn.get_txid())
    return method.returns.type.decode(base64.b64decode(res["logs"][0])[4:])


def escrow_total(a, app_id, who: Acct) -> int:
    method = abi.Method.from_signature("escrow_total()uint64")
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    txn = transaction.ApplicationNoOpTxn(who.address, sp, app_id, app_args=[method.get_selector()])
    wait(a, a.send_transaction(txn.sign(who.private_key)))
    res = a.pending_transaction_info(txn.get_txid())
    return method.returns.type.decode(base64.b64decode(res["logs"][0])[4:])


def main() -> int:
    a, dispenser = connect()
    print("dispenser:", dispenser.address[:12], "round:", a.status()["last-round"])

    author = Acct.generate()
    challenger = Acct.generate()
    arbiter = Acct.generate()
    stranger = Acct.generate()
    for acct in (author, challenger, arbiter, stranger):
        send_pay(a, dispenser, acct.address, 8_000_000)
    send_pay(a, dispenser, Acct.generate().address, 1_000_000)

    app_id = deploy(a, dispenser, [arbiter.address, dispenser.address, ONE,
                                   10000, SLASH_SHARE_BPS, RESOLVE_WINDOW])
    print("app id:", app_id, "app address:", app_addr(app_id))
    send_pay(a, dispenser, app_addr(app_id), 2_000_000)
    info = a.account_info(app_addr(app_id))
    print("app min-balance:", info["min-balance"], "| balance:", info["amount"])

    everyone = [author.address, challenger.address, arbiter.address, dispenser.address]
    results = []

    # ---------------- A: slash
    pid = 1
    group_post(a, author, app_id, pid, 60, accounts=everyone)
    assert escrow_total(a, app_id, dispenser) == BOND, "escrow after post"
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    assert escrow_total(a, app_id, dispenser) == BOND + CHAL, "escrow after challenge"
    ch_before = bal(a, challenger.address)
    tr_before = bal(a, dispenser.address)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, True],
         boxes=[box_key(pid)], accounts=everyone)
    expect_ch = ch_before + CHAL + BOND * SLASH_SHARE_BPS // 10000
    ok_slash = bal(a, challenger.address) == expect_ch
    ok_escrow = escrow_total(a, app_id, dispenser) == 0
    results.append(("A slash: challenger gets bond + half of author bond", ok_slash and ok_escrow,
                    (bal(a, challenger.address) - ch_before, bal(a, dispenser.address) - tr_before)))

    # ---------------- B: release
    pid = 2
    group_post(a, author, app_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    au_before = bal(a, author.address)
    ch_before = bal(a, challenger.address)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False],
         boxes=[box_key(pid)], accounts=everyone)
    # author also pays nothing here (arbiter pays the fee); exact refund expected
    ok_release = (bal(a, author.address) == au_before + BOND and
                  bal(a, challenger.address) == ch_before + CHAL and
                  escrow_total(a, app_id, dispenser) == 0)
    results.append(("B release: both bonds refunded", ok_release,
                    (bal(a, author.address) - au_before, bal(a, challenger.address) - ch_before)))

    # ---------------- C: reclaim after window
    pid = 3
    au_before = bal(a, author.address)
    # (1) early rejection: a far deadline guarantees the attempt is really early
    group_post(a, author, app_id, pid, 100_000, accounts=everyone)
    early = ""
    try:
        call(a, author, app_id, "reclaim(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
        early = "UNEXPECTED SUCCESS"
    except Exception as e:
        early = "rejected: " + str(e).split("\n")[0][:60]
    # clean up that post so the final escrow total returns to zero
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False],
         boxes=[box_key(pid)], accounts=everyone)
    early_ok = early.startswith("rejected")

    # (2) expiry: one-round window, then wait on the ledger clock
    pid = 6
    group_post(a, author, app_id, pid, 1, accounts=everyone)
    deadline = post_state(a, app_id, pid, [author, challenger, arbiter, dispenser])[5]
    wait_ledger(a, deadline, poker=dispenser)
    au_pre_reclaim = bal(a, author.address)
    call(a, author, app_id, "reclaim(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    refund = bal(a, author.address) - au_pre_reclaim
    # the author pays the 3000 call fee, so the refund is bond - fee
    ok_reclaim = refund == BOND - 3000 and escrow_total(a, app_id, dispenser) == 0
    results.append((f"C reclaim: early call {early}; after deadline refund={refund}",
                    ok_reclaim and early_ok, (early_ok, refund)))

    # ---------------- D: arbiter stalls -> permissionless release
    pid = 4
    group_post(a, author, app_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    resolve_deadline = post_state(a, app_id, pid, [author, challenger, arbiter, dispenser])[6]
    wait_ledger(a, resolve_deadline, poker=dispenser)
    au_before = bal(a, author.address)
    ch_before = bal(a, challenger.address)
    call(a, stranger, app_id, "release_stale(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    ok_stale = (bal(a, author.address) == au_before + BOND and
                bal(a, challenger.address) == ch_before + CHAL and
                escrow_total(a, app_id, dispenser) == 0)
    results.append(("D stale: anyone refunds both after resolve window", ok_stale,
                    (bal(a, author.address) - au_before, bal(a, challenger.address) - ch_before)))

    # ---------------- E: non-arbiter cannot settle
    pid = 5
    group_post(a, author, app_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    guard = ""
    try:
        call(a, author, app_id, "settle(uint64,bool)void", [pid, True],
             boxes=[box_key(pid)], accounts=everyone)
        guard = "UNEXPECTED SUCCESS"
    except Exception as e:
        guard = "rejected: " + str(e).split("\n")[0][:60]
    held = escrow_total(a, app_id, dispenser)
    results.append(("E guard: non-arbiter settle " + guard, guard.startswith("rejected") and held == BOND + CHAL, held))

    # ---------------- solvency invariant
    app_bal = bal(a, app_addr(app_id))
    min_bal = a.account_info(app_addr(app_id))["min-balance"]
    float_mbr = app_bal - held
    results.append(("F solvency: app balance = escrow + MBR float, float > 0",
                    app_bal >= held and float_mbr > 0, (held, app_bal, min_bal)))

    print()
    print("escrow held (post 5 still open+challenged):", held)
    print("app balance:", app_bal, "| min-balance:", min_bal, "| MBR float above escrow:", float_mbr)
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
