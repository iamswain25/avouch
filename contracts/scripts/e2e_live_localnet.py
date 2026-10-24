"""Localnet e2e for the living-bond contract (PostBondLive): challenge at any time.

  A guilty      post -> challenge -> settle(guilty): partial forfeit, post stays OPEN
  B innocent    challenge -> settle(not guilty): challenger refunded, bond untouched
  C stale       challenge -> arbiter silent -> release_stale: challenger refunded, OPEN
  D close       author closes: bond returned, box gone, escrow drained
  E any time    a challenge lands long after posting (no window exists at all)
  F guards      non-arbiter settle, non-author close, double challenge, challenge after close

Run: .venv/bin/python scripts/e2e_live_localnet.py
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from algosdk import abi, account, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
ART = Path(__file__).resolve().parent.parent / "src" / "artifacts"

ONE = 1_000_000
BOND = 2 * ONE            # 2 ALGO so a 50% forfeit still leaves >= min_bond
MIN_BOND = 1 * ONE
CHAL = 2 * ONE            # 1:1
SHARE_BPS = 5000
FORFEIT_BPS = 5000
RESOLVE_WINDOW = 4
FEE = 1000

GET_POST_SIG = "get_post(uint64)(address,address,byte[],uint64,uint64,uint64,uint8,uint64,uint64)"


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


def wait(a, txid):
    transaction.wait_for_confirmation(a, txid, 10)


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


def deploy(a, creator) -> int:
    src = json.loads((ART / "PostBondLive.arc56.json").read_text())
    approval = base64.b64decode(src["byteCode"]["approval"])
    clear = base64.b64decode(src["byteCode"]["clear"])
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    method = abi.Method.from_signature("create(address,address,uint64,uint64,uint64,uint64,uint64)void")
    values = [ADDR_ARBITER, creator.address, MIN_BOND, 10000, SHARE_BPS, FORFEIT_BPS, RESOLVE_WINDOW]
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    txn = transaction.ApplicationCreateTxn(
        creator.address, sp, transaction.OnComplete.NoOpOC, approval, clear,
        transaction.StateSchema(7, 2), transaction.StateSchema(0, 0), app_args=app_args,
    )
    wait(a, a.send_transaction(txn.sign(creator.private_key)))
    return a.pending_transaction_info(txn.get_txid())["application-index"]


def call(a, sender, app_id, sig: str, values: list, *, boxes=(), accounts=(), fee=3000):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    box_refs = [(app_id, b) if isinstance(b, (bytes, bytearray)) else b for b in boxes]
    txn = transaction.ApplicationNoOpTxn(
        sender.address, sp, app_id, app_args=app_args, accounts=list(accounts), boxes=box_refs,
    )
    wait(a, a.send_transaction(txn.sign(sender.private_key)))
    return txn.get_txid()


def read_view(a, who, app_id, sig: str, values=(), boxes=()):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    box_refs = [(app_id, b) if isinstance(b, (bytes, bytearray)) else b for b in boxes]
    txn = transaction.ApplicationNoOpTxn(who.address, sp, app_id, app_args=app_args, boxes=box_refs)
    wait(a, a.send_transaction(txn.sign(who.private_key)))
    res = a.pending_transaction_info(txn.get_txid())
    return method.returns.type.decode(base64.b64decode(res["logs"][0])[4:])


def escrow_total(a, who, app_id) -> int:
    return read_view(a, who, app_id, "escrow_total()uint64")


def post_state(a, who, app_id, pid):
    return read_view(a, who, app_id, GET_POST_SIG, [pid], boxes=[box_key(pid)])


def group_post(a, author, app_id, pid, bond=BOND, accounts=()):
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    pay = transaction.PaymentTxn(author.address, sp, app_addr(app_id), bond)
    method = abi.Method.from_signature("post_bond(pay,uint64,byte[])void")
    ch = f"sha256:live-post-{pid}-{int(time.time())}".encode()
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 3000
    appcall = transaction.ApplicationNoOpTxn(
        author.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(pid), method.args[2].type.encode(ch)],
        accounts=list(accounts), boxes=[(app_id, box_key(pid))],
    )
    transaction.assign_group_id([pay, appcall])
    wait(a, a.send_transactions([pay.sign(author.private_key), appcall.sign(author.private_key)]))


def group_challenge(a, challenger, app_id, pid, amount=CHAL, accounts=()):
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
        app_args=[method.get_selector(), method.args[1].type.encode(pid)],
        accounts=list(accounts), boxes=[(app_id, box_key(pid))],
    )
    transaction.assign_group_id([pay, appcall])
    wait(a, a.send_transactions([pay.sign(challenger.private_key), appcall.sign(challenger.private_key)]))


def poke(a, acct):
    sp = a.suggested_params()
    txn = transaction.PaymentTxn(acct.address, sp, acct.address, 0)
    wait(a, a.send_transaction(txn.sign(acct.private_key)))


def ledger_now(a) -> int:
    return a.block_info(a.status()["last-round"])["block"]["ts"]


def wait_ledger(a, ts: int, poker, timeout_s: float = 60.0):
    start = time.time()
    while ledger_now(a) < ts + 1:
        if time.time() - start > timeout_s:
            raise TimeoutError(f"ledger clock stuck at {ledger_now(a)}")
        poke(a, poker)
        time.sleep(0.2)


def main() -> int:
    a, dispenser = connect()
    print("dispenser:", dispenser.address[:12], "round:", a.status()["last-round"])

    author = Acct.generate()
    challenger = Acct.generate()
    arbiter = Acct.generate()
    stranger = Acct.generate()
    for acct in (author, challenger, arbiter, stranger):
        send_pay(a, dispenser, acct.address, 20_000_000)

    global ADDR_ARBITER
    ADDR_ARBITER = arbiter.address

    app_id = deploy(a, dispenser)
    print("app id:", app_id)
    send_pay(a, dispenser, app_addr(app_id), 5_000_000)
    everyone = [author.address, challenger.address, arbiter.address, dispenser.address]
    results = []

    # ---------------- E: challenge long after posting (no window exists)
    pid = 5
    group_post(a, author, app_id, pid, accounts=everyone)
    for _ in range(6):
        poke(a, dispenser)
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    st = post_state(a, dispenser, app_id, pid)
    ok_anytime = int(st[6]) == 1  # CHALLENGED
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False], boxes=[box_key(pid)], accounts=everyone)
    results.append(("E challenge after many rounds (no window)", ok_anytime, f"status={int(st[6])}"))

    # ---------------- A: guilty -> partial forfeit, post stays OPEN
    pid = 1
    group_post(a, author, app_id, pid, accounts=everyone)
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    ch_before = bal(a, challenger.address)
    tr_before = bal(a, dispenser.address)
    esc_before = escrow_total(a, dispenser, app_id)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, True], boxes=[box_key(pid)], accounts=everyone)
    st = post_state(a, dispenser, app_id, pid)
    bond_left, closed = int(st[3]), int(st[6])
    ch_delta = bal(a, challenger.address) - ch_before
    forfeit = BOND * FORFEIT_BPS // 10000
    expected_ch = CHAL + forfeit * SHARE_BPS // 10000
    ok_guilty = (closed == 0 and bond_left == BOND - forfeit and ch_delta == expected_ch
                 and escrow_total(a, dispenser, app_id) == esc_before - CHAL - forfeit)
    results.append(("A guilty: partial forfeit, post OPEN with reduced bond", ok_guilty,
                    f"bond {BOND}->{bond_left}, challenger +{ch_delta} (expected {expected_ch})"))

    # ---------------- B: second challenge on the same post, innocent
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    ch_before = bal(a, challenger.address)
    esc_before = escrow_total(a, dispenser, app_id)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False], boxes=[box_key(pid)], accounts=everyone)
    st = post_state(a, dispenser, app_id, pid)
    ok_second = (int(st[6]) == 0 and int(st[3]) == bond_left and
                 bal(a, challenger.address) - ch_before == CHAL and
                 escrow_total(a, dispenser, app_id) == esc_before - CHAL)
    results.append(("B second challenge, innocent: refunded, bond untouched", ok_second,
                    f"bond={int(st[3])} rulings={int(st[7])}"))

    # ---------------- C: arbiter stalls -> release_stale
    group_challenge(a, challenger, app_id, pid, accounts=everyone)
    # struct order: author, challenger, hash, bond, challenger_bond, resolve_deadline, status, rulings, forfeited
    resolve_deadline = int(post_state(a, dispenser, app_id, pid)[5])
    wait_ledger(a, resolve_deadline, dispenser)
    ch_before = bal(a, challenger.address)
    call(a, stranger, app_id, "release_stale(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    st = post_state(a, dispenser, app_id, pid)
    ok_stale = int(st[6]) == 0 and bal(a, challenger.address) - ch_before == CHAL
    results.append(("C stale: anyone refunds challenger, post stays OPEN", ok_stale,
                    f"status={int(st[6])} delta={bal(a, challenger.address) - ch_before}"))

    # ---------------- D: author closes -> bond back, box gone
    au_before = bal(a, author.address)
    esc_before = escrow_total(a, dispenser, app_id)
    call(a, author, app_id, "close(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    gone = False
    try:
        post_state(a, dispenser, app_id, pid)
    except Exception:
        gone = True
    ok_close = (gone and bal(a, author.address) - au_before == bond_left - 3000  # author pays the call fee
                and escrow_total(a, dispenser, app_id) == esc_before - bond_left)
    results.append(("D close: bond returned, box deleted", ok_close,
                    f"box_gone={gone} author +{bal(a, author.address) - au_before}"))

    # ---------------- F: guards
    pid = 6
    group_post(a, author, app_id, pid, accounts=everyone)
    g1 = g2 = g3 = g4 = False
    try:  # non-arbiter settle
        group_challenge(a, challenger, app_id, pid, accounts=everyone)
        call(a, author, app_id, "settle(uint64,bool)void", [pid, True], boxes=[box_key(pid)], accounts=everyone)
    except Exception:
        g1 = True
    try:  # challenge while already challenged
        group_challenge(a, stranger, app_id, pid, accounts=everyone)
    except Exception:
        g2 = True
    try:  # non-author close
        call(a, challenger, app_id, "close(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    except Exception:
        g3 = True
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False], boxes=[box_key(pid)], accounts=everyone)
    call(a, author, app_id, "close(uint64)void", [pid], boxes=[box_key(pid)], accounts=everyone)
    try:  # challenge after close
        group_challenge(a, challenger, app_id, pid, accounts=everyone)
    except Exception:
        g4 = True
    results.append(("F guards: non-arbiter/non-author/double-challenge/after-close",
                    g1 and g2 and g3 and g4, f"{g1},{g2},{g3},{g4}"))

    # cleanup: close the post from scenario E so the final escrow invariant is 0
    call(a, author, app_id, "close(uint64)void", [5], boxes=[box_key(5)], accounts=everyone)
    results.append(("G final: escrow drained after every post closed",
                    escrow_total(a, dispenser, app_id) == 0,
                    f"escrow={escrow_total(a, dispenser, app_id)}"))

    print()
    ok_all = True
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name, "|", detail)
        ok_all &= bool(ok)
    print()
    print("escrow left:", escrow_total(a, dispenser, app_id), "| app balance:", bal(a, app_addr(app_id)))
    print("RESULT:", "ALL PASS" if ok_all else "FAILURES PRESENT")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
