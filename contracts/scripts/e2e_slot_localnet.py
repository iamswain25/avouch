"""Localnet e2e for the slot auction (v4): stake/unstake, no arbiter, no window, no ruling.

  A open        a first stake takes the slot; a second backer raises the total
  B free exit   a challenger that never took the slot walks away with its whole stake
  C displacement  a bigger challenger flips the slot in the crossing transaction:
                  incumbent frozen with a 50% refund pool, 25% of its stake to the winner's
                  backers (pull), 25% to the treasury, epoch + 1
  D reward claim  the winner's backers collect the credit when they leave, and pay the 50%
                  exit fee on their own stake only
  E succession  the top leaves -> the best known challenger is promoted in the same call
  F reopen      an emptied slot needs min_stake again; below it the stake is refused
  G guards      staking into a displaced hash, over-unstaking, and a short hash are refused
  H invariant   escrow == outstanding stakes (dust from integer division only)

Run: .venv/bin/python scripts/e2e_slot_localnet.py
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from algosdk import abi, account, encoding, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
ART = Path(__file__).resolve().parent.parent / "src" / "artifacts"

ONE = 1_000_000
MIN_STAKE = 100_000
F_BPS = 5000
S_BPS = 5000
FEE = 1000           # single outer txn
STAKE_FEE = 2000     # outer 1000 + one possible inner (treasury)
UNSTAKE_FEE = 3000   # outer 1000 + up to two inner (staker, treasury)

H1 = bytes([0x11]) * 32
H2 = bytes([0x22]) * 32
H3 = bytes([0x33]) * 32
H4 = bytes([0x44]) * 32

TREASURY: dict[str, str] = {}   # the app pays the treasury with an inner txn, so it must be in accounts

SLOT_SIG = "get_slot(uint64)(byte[],uint64,byte[],uint64,uint64)"
ANSWER_SIG = "get_answer(uint64,byte[])(uint64,uint64,bool,uint64,uint64,uint64)"
STAKE_SIG = "get_stake(uint64,address)(byte[],uint64,uint64)"
IDX_TOP_HASH, IDX_TOP_TOTAL, IDX_SECOND_HASH, IDX_SECOND_TOTAL, IDX_EPOCH = 0, 1, 2, 3, 4
IDX_TOTAL, IDX_ACC, IDX_SETTLED, IDX_ACC_SETTLE, IDX_SETTLE_TOTAL, IDX_REFUND = range(6)


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


def slot_key(sid: int) -> bytes:
    return b"s" + sid.to_bytes(8, "big")


def bval(x) -> bytes:
    return bytes(x) if isinstance(x, (list, bytearray)) else x


def answer_key(sid: int, h: bytes) -> bytes:
    return b"a" + sid.to_bytes(8, "big") + bval(h)


def stake_key(sid: int, addr: str) -> bytes:
    return b"k" + sid.to_bytes(8, "big") + encoding.decode_address(addr)


def deploy(a, creator, treasury: "Acct") -> int:
    src = json.loads((ART / "SlotAuction.arc56.json").read_text())
    approval = base64.b64decode(src["byteCode"]["approval"])
    clear = base64.b64decode(src["byteCode"]["clear"])
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    method = abi.Method.from_signature("create(address,uint64,uint64,uint64)void")
    values = [treasury.address, F_BPS, S_BPS, MIN_STAKE]
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    txn = transaction.ApplicationCreateTxn(
        creator.address, sp, transaction.OnComplete.NoOpOC, approval, clear,
        transaction.StateSchema(4, 1), transaction.StateSchema(0, 0), app_args=app_args,
        extra_pages=1,   # reward/claim push the approval past one page

    )
    wait(a, a.send_transaction(txn.sign(creator.private_key)))
    return a.pending_transaction_info(txn.get_txid())["application-index"]


def stake(a, who: "Acct", app_id: int, sid: int, h: bytes, amount: int, boxes=()):
    # a stake that can displace must reference the incumbent's answer box; a client reads
    # the slot first and adds it, which is what this helper does
    keys = list(boxes)
    try:
        slot = slot_state(a, who, app_id, sid)
        if slot[IDX_TOP_TOTAL] and bval(slot[IDX_TOP_HASH]) != h:
            k = answer_key(sid, slot[IDX_TOP_HASH])
            if k not in keys:
                keys.append(k)
    except Exception:
        pass  # no slot box yet
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    pay = transaction.PaymentTxn(who.address, sp, app_addr(app_id), amount)
    method = abi.Method.from_signature("stake(pay,uint64,byte[])void")
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = STAKE_FEE
    appcall = transaction.ApplicationNoOpTxn(
        who.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(sid), method.args[2].type.encode(h)],
        boxes=[(app_id, b) for b in keys],
        accounts=[TREASURY["addr"]] if TREASURY else [],
    )
    transaction.assign_group_id([pay, appcall])
    wait(a, a.send_transactions([pay.sign(who.private_key), appcall.sign(who.private_key)]))


def unstake(a, who: "Acct", app_id: int, sid: int, amount: int, boxes=(), fee=UNSTAKE_FEE):
    method = abi.Method.from_signature("unstake(uint64,uint64)void")
    app_args = [method.get_selector(), method.args[0].type.encode(sid), method.args[1].type.encode(amount)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    txn = transaction.ApplicationNoOpTxn(
        who.address, sp, app_id, app_args=app_args, boxes=[(app_id, b) for b in boxes],
        accounts=[TREASURY["addr"]] if TREASURY else [],
    )
    wait(a, a.send_transaction(txn.sign(who.private_key)))


def read_view(a, who, app_id, sig: str, values=(), boxes=()):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    txn = transaction.ApplicationNoOpTxn(
        who.address, sp, app_id, app_args=app_args, boxes=[(app_id, b) for b in boxes],
    )
    wait(a, a.send_transaction(txn.sign(who.private_key)))
    res = a.pending_transaction_info(txn.get_txid())
    return method.returns.type.decode(base64.b64decode(res["logs"][0])[4:])


def slot_state(a, who, app_id, sid):
    return read_view(a, who, app_id, SLOT_SIG, [sid], boxes=[slot_key(sid)])


def answer_state(a, who, app_id, sid, h):
    return read_view(a, who, app_id, ANSWER_SIG, [sid, h], boxes=[answer_key(sid, h)])


def stake_state(a, who, app_id, sid, addr):
    return read_view(a, who, app_id, STAKE_SIG, [sid, addr], boxes=[stake_key(sid, addr)])


def escrow_total(a, who, app_id) -> int:
    return int(read_view(a, who, app_id, "escrow_total()uint64"))




def call_method(a, who, app_id, sig, values, boxes=(), fee=2000):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    txn = transaction.ApplicationNoOpTxn(
        who.address, sp, app_id, app_args=app_args, boxes=[(app_id, b) for b in boxes],
    )
    wait(a, a.send_transaction(txn.sign(who.private_key)))


def group_reward(a, payer, app_id, sid, amount, accounts=()):
    # the reward writes the answer box of whatever hash holds the slot, so a client reads the
    # slot first and lists that box (the contract cannot guess it)
    boxes = [slot_key(sid)]
    try:
        slot = slot_state(a, payer, app_id, sid)
        if int(slot[IDX_TOP_TOTAL]) > 0:
            boxes.append(answer_key(sid, bval(slot[IDX_TOP_HASH])))
    except Exception:
        pass
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = FEE
    pay = transaction.PaymentTxn(payer.address, sp, app_addr(app_id), amount)
    method = abi.Method.from_signature("reward(pay,uint64)void")
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 2000
    call = transaction.ApplicationNoOpTxn(
        payer.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(sid)],
        boxes=[(app_id, b) for b in boxes],
        accounts=list(accounts),
    )
    transaction.assign_group_id([pay, call])
    wait(a, a.send_transactions([pay.sign(payer.private_key), call.sign(payer.private_key)]))

def fails(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def main() -> int:
    a, dispenser = connect()
    treasury = Acct.generate()
    alice, bob, carol, dave, erin, frank = (Acct.generate() for _ in range(6))
    people = (alice, bob, carol, dave, erin, frank)
    for acct in people:
        send_pay(a, dispenser, acct.address, 30 * ONE)

    TREASURY["addr"] = treasury.address
    app_id = deploy(a, dispenser, treasury)
    send_pay(a, dispenser, app_addr(app_id), 10 * ONE)   # box MBR float for the prototype
    print("app id:", app_id, "| round:", a.status()["last-round"])
    results = []

    sid = 1
    s_boxes = [slot_key(sid)]

    # ---------------- A: first stake takes the slot, second backer raises it
    before = bal(a, alice.address)
    stake(a, alice, app_id, sid, H1, ONE, boxes=[slot_key(sid), answer_key(sid, H1), stake_key(sid, alice.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    ok_a1 = (bval(slot[IDX_TOP_HASH]) == H1 and int(slot[IDX_TOP_TOTAL]) == ONE
             and int(slot[IDX_EPOCH]) == 1 and before - bal(a, alice.address) == ONE + FEE + STAKE_FEE)
    stake(a, bob, app_id, sid, H1, 500_000, boxes=[slot_key(sid), answer_key(sid, H1), stake_key(sid, bob.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    ans1 = answer_state(a, dispenser, app_id, sid, H1)
    ok_a2 = int(slot[IDX_TOP_TOTAL]) == 1_500_000 and int(ans1[IDX_TOTAL]) == 1_500_000
    results.append(("A open: alice takes the slot, bob backs the same hash", ok_a1 and ok_a2,
                    f"top={int(slot[IDX_TOP_TOTAL])} epoch={int(slot[IDX_EPOCH])}"))

    # ---------------- B: a challenger that never takes the slot exits for free
    stake(a, carol, app_id, sid, H2, 500_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, carol.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    second_ok = bval(slot[IDX_SECOND_HASH]) == H2 and int(slot[IDX_SECOND_TOTAL]) == 500_000
    before = bal(a, carol.address)
    unstake(a, carol, app_id, sid, 500_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, carol.address)])
    got = bal(a, carol.address) - before
    results.append(("B free exit: losing challenger gets the whole stake back, no fee",
                    second_ok and got == 500_000 - UNSTAKE_FEE and int(escrow_total(a, dispenser, app_id)) == 1_500_000,
                    f"refund={got + UNSTAKE_FEE} second={bval(slot[IDX_SECOND_HASH]) == H2}"))

    # ---------------- C: crossing the line displaces the incumbent in that transaction
    before = bal(a, carol.address)
    stake(a, carol, app_id, sid, H2, 500_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, carol.address)])
    t_before = bal(a, treasury.address)
    stake(a, dave, app_id, sid, H2, 1_100_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, dave.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    lost = answer_state(a, dispenser, app_id, sid, H1)
    won = answer_state(a, dispenser, app_id, sid, H2)
    fee_h1 = 1_500_000 * F_BPS // 10_000          # 750,000
    reward = fee_h1 * S_BPS // 10_000             # 375,000
    acc_expect = reward * 1_000_000 // 1_600_000  # 234,375
    ok_c = (bool(lost[IDX_SETTLED]) and int(lost[IDX_REFUND]) == 1_500_000 - fee_h1
            and bval(slot[IDX_TOP_HASH]) == H2 and int(slot[IDX_TOP_TOTAL]) == 1_600_000
            and int(slot[IDX_EPOCH]) == 2 and int(won[IDX_ACC]) == acc_expect
            and bal(a, treasury.address) - t_before == fee_h1 - reward)
    results.append(("C displacement: incumbent frozen, winner credited, treasury paid, epoch 2",
                    ok_c, f"refund_pool={int(lost[IDX_REFUND])} acc={int(won[IDX_ACC])} "
                          f"treasury+={bal(a, treasury.address) - t_before} epoch={int(slot[IDX_EPOCH])}"))

    # ---------------- D: the incumbent's backers collect their 50%, the winners collect the credit
    before = bal(a, alice.address)
    unstake(a, alice, app_id, sid, ONE, boxes=[slot_key(sid), answer_key(sid, H1), stake_key(sid, alice.address)])
    alice_got = bal(a, alice.address) - before + UNSTAKE_FEE
    before = bal(a, bob.address)
    unstake(a, bob, app_id, sid, 500_000, boxes=[slot_key(sid), answer_key(sid, H1), stake_key(sid, bob.address)])
    bob_got = bal(a, bob.address) - before + UNSTAKE_FEE
    before = bal(a, carol.address)
    t_before = bal(a, treasury.address)
    unstake(a, carol, app_id, sid, 500_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, carol.address)])
    carol_net = bal(a, carol.address) - before
    carol_paid_fee = bal(a, treasury.address) - t_before
    carol_claim = 500_000 * acc_expect // 1_000_000
    ok_d = (alice_got == 500_000 and bob_got == 250_000
            and carol_net == 500_000 - 250_000 + carol_claim - UNSTAKE_FEE
            and carol_paid_fee == 250_000)
    results.append(("D splits: incumbents keep 50%, winners take the credit, leavers pay 50%",
                    ok_d, f"alice={alice_got} bob={bob_got} carol={carol_net + UNSTAKE_FEE} "
                          f"(claim {carol_claim}, fee {carol_paid_fee})"))

    # ---------------- E: the best challenger is promoted when the top walks away
    stake(a, erin, app_id, sid, H3, 400_000, boxes=[slot_key(sid), answer_key(sid, H3), stake_key(sid, erin.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    second_ok = bval(slot[IDX_SECOND_HASH]) == H3
    before = bal(a, dave.address)
    t_before = bal(a, treasury.address)
    unstake(a, dave, app_id, sid, 1_100_000, boxes=[slot_key(sid), answer_key(sid, H2), stake_key(sid, dave.address)])
    dave_net = bal(a, dave.address) - before
    slot = slot_state(a, dispenser, app_id, sid)
    dave_claim = 1_100_000 * acc_expect // 1_000_000
    ok_e = (second_ok and bval(slot[IDX_TOP_HASH]) == H3 and int(slot[IDX_TOP_TOTAL]) == 400_000
            and int(slot[IDX_EPOCH]) == 3
            and dave_net == 1_100_000 - 550_000 + dave_claim - UNSTAKE_FEE
            and bal(a, treasury.address) - t_before == 550_000)
    results.append(("E succession: top leaves -> the best challenger holds the slot, epoch 3",
                    ok_e, f"top={bval(slot[IDX_TOP_HASH]).hex()[:4]} total={int(slot[IDX_TOP_TOTAL])} "
                          f"dave={dave_net + UNSTAKE_FEE}"))

    # ---------------- F: the slot empties, then needs min_stake again
    unstake(a, erin, app_id, sid, 400_000, boxes=[slot_key(sid), answer_key(sid, H3), stake_key(sid, erin.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    emptied = slot[IDX_TOP_TOTAL] == 0 and bval(slot[IDX_TOP_HASH]) == b"" and int(slot[IDX_EPOCH]) == 4
    too_small = fails(lambda: stake(a, frank, app_id, sid, H4, MIN_STAKE - 1,
                                    boxes=[slot_key(sid), answer_key(sid, H4), stake_key(sid, frank.address)]))
    stake(a, frank, app_id, sid, H4, MIN_STAKE,
          boxes=[slot_key(sid), answer_key(sid, H4), stake_key(sid, frank.address)])
    slot = slot_state(a, dispenser, app_id, sid)
    ok_f = emptied and too_small and bval(slot[IDX_TOP_HASH]) == H4 and int(slot[IDX_TOP_TOTAL]) == MIN_STAKE
    results.append(("F reopen: emptied slot refuses below the floor, accepts it", ok_f,
                    f"emptied={emptied} refused_small={too_small} top={int(slot[IDX_TOP_TOTAL])}"))

    # ---------------- G: guards
    g_frozen_hash = fails(lambda: stake(a, erin, app_id, sid, H1, 200_000,
                                        boxes=[slot_key(sid), answer_key(sid, H1), stake_key(sid, erin.address)]))
    g_over = fails(lambda: unstake(a, frank, app_id, sid, MIN_STAKE + 1,
                                   boxes=[slot_key(sid), answer_key(sid, H4), stake_key(sid, frank.address)]))
    g_short_hash = fails(lambda: stake(a, erin, app_id, sid, b"\x55" * 16, 200_000,
                                       boxes=[slot_key(sid), stake_key(sid, erin.address)]))
    results.append(("G guards: displaced hash, unknown 16-byte hash, over-unstake refused",
                    g_frozen_hash and g_over and g_short_hash,
                    f"frozen={g_frozen_hash} over={g_over} short={g_short_hash}"))

    # ---------------- H: invariant - only integer-division dust stays behind
    unstake(a, frank, app_id, sid, MIN_STAKE, boxes=[slot_key(sid), answer_key(sid, H4), stake_key(sid, frank.address)])
    residual = escrow_total(a, dispenser, app_id)
    ok_h = 0 <= residual < 10
    results.append(("H invariant: escrow is empty apart from division dust", ok_h, f"residual={residual} uALGO"))

    # ---------------- I: read revenue - a batched reward accrues to the answer's backers
    sid2 = 2
    stake(a, alice, app_id, sid2, H1, ONE, boxes=[slot_key(sid2), answer_key(sid2, H1), stake_key(sid2, alice.address)])
    stake(a, bob, app_id, sid2, H1, ONE, boxes=[slot_key(sid2), answer_key(sid2, H1), stake_key(sid2, bob.address)])
    esc_before = escrow_total(a, dispenser, app_id)
    group_reward(a, dispenser, app_id, sid2, 20_000)
    ans2 = answer_state(a, dispenser, app_id, sid2, H1)
    acc_expect = 20_000 * 1_000_000 // 2_000_000
    ok_i = int(ans2[IDX_ACC]) == acc_expect and escrow_total(a, dispenser, app_id) == esc_before + 20_000
    results.append(("I read revenue: a batched reward raises the answer accumulator",
                    ok_i, f"acc={int(ans2[IDX_ACC])} escrow+{escrow_total(a, dispenser, app_id) - esc_before}"))

    # ---------------- J: each backer claims a pro-rata share, and only once
    before_a = bal(a, alice.address)
    call_method(a, alice, app_id, "claim(uint64)void", [sid2], boxes=[slot_key(sid2), answer_key(sid2, H1), stake_key(sid2, alice.address)])
    alice_got = bal(a, alice.address) - before_a + 2000
    before_b = bal(a, bob.address)
    call_method(a, bob, app_id, "claim(uint64)void", [sid2], boxes=[slot_key(sid2), answer_key(sid2, H1), stake_key(sid2, bob.address)])
    bob_got = bal(a, bob.address) - before_b + 2000
    twice = fails(lambda: call_method(a, alice, app_id, "claim(uint64)void", [sid2],
                                      boxes=[slot_key(sid2), answer_key(sid2, H1), stake_key(sid2, alice.address)]))
    ok_j = alice_got == 10_000 and bob_got == 10_000 and twice
    results.append(("J claim: pro-rata read revenue, second claim refused", ok_j,
                    f"alice={alice_got} bob={bob_got} twice_refused={twice}"))

    # ---------------- K: dust-sized rewards and empty slots are refused
    empty_slot = fails(lambda: group_reward(a, dispenser, app_id, 99, 20_000))
    tiny = fails(lambda: group_reward(a, dispenser, app_id, sid2, 1))
    results.append(("K guards: reward on an empty slot and a dust reward are refused",
                    empty_slot and tiny, f"empty={empty_slot} dust={tiny}"))

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
