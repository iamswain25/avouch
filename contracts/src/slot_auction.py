"""Slot auction (v4): one answer per question, ranked by money, no verdicts.

v1-v3 had a challenge window, an arbiter, a jury and a ruling. This contract has none of
them. It has two operations:

    stake(slot_id, content_hash, pay)    put money behind an answer
    unstake(slot_id, amount)             take money out

A question is a slot. An answer is a content hash. The answer that holds the slot is the
one with the most money behind it; ties go to whoever got there first. A challenge is not
an object, it is a number: when a different hash's total passes the incumbent's, the slot
flips inside the transaction that crossed the line. Replacement and retirement are the
same event seen from two sides, so there is one fee, not two.

    leaving the top (displaced, or you withdraw your own top stake)   f = 50% of the stake
        half of f to the side that took the slot (s = 50%), half to the treasury
        nobody took it (you retired) -> all of f to the treasury
    leaving a hash that already lost    nothing more; you paid f when it lost
    leaving money that never held the slot    no fee, full refund

Deterrence is not a goal. The protocol does not try to make lying unprofitable; it
publishes the price of every answer (stake, epoch, replacement count) and lets readers
price it. That is why the only two parameters are f and the chain floor for a new slot.

Layout

    slot    "s" + id          Slot   {top_hash, top_total, second_hash, second_total, epoch}
    answer  "a" + id + hash   Answer {total, acc, settled, acc_at_settle, settle_total,
                                      refund_pool}
    stake   "k" + id + addr   Stake  {hash, amount, debt}

Reward distribution is pull-based. A displacement credits the winning hash's accumulator
(reward per unit, scaled by ACC) and leaves the losing side a refund pool; each backer
reads its own stake box and collects on unstake, so nobody is ever iterated over and the
16-inner-transaction limit never comes up. A hash can win at most once (after winning it
is the top, and losing freezes it), which bounds the accumulator and keeps the
amount*acc product well inside uint64.

Only a hash's first claim to a slot can be displaced; once an answer is frozen the only
exit is unstake at the refund rate. A slot that loses its last top stake is empty until
some hash's accumulated total reaches the floor again.

Rounding leaves a few microALGO of dust per displacement in the app account. It is not
lost; escrow_total() reports the obligations, and the difference is float. Production
would need to charge box MBR to the staker (see README) instead of funding it from the
app account.

Run: puyapy src/slot_auction.py --out-dir src/artifacts
"""

from algopy import (
    ARC4Contract,
    Account,
    BoxMap,
    Bytes,
    Global,
    GlobalState,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
)

ACC = 1_000_000        # reward-per-unit scale; keeps amount*acc inside uint64 while
                       # resolving rewards down to microALGO for stakes of a few ALGO
MAX_ACC = 4_294_967_296  # 2^32: a hard ceiling so amount*acc can never overflow
BPS = 10_000


class Slot(arc4.Struct):
    top_hash: arc4.DynamicBytes
    top_total: arc4.UInt64
    second_hash: arc4.DynamicBytes
    second_total: arc4.UInt64
    epoch: arc4.UInt64


class Answer(arc4.Struct):
    total: arc4.UInt64
    acc: arc4.UInt64
    settled: arc4.Bool
    acc_at_settle: arc4.UInt64
    settle_total: arc4.UInt64
    refund_pool: arc4.UInt64


class Stake(arc4.Struct):
    hash: arc4.DynamicBytes
    amount: arc4.UInt64
    debt: arc4.UInt64


class SlotAuction(ARC4Contract):
    def __init__(self) -> None:
        self.treasury = GlobalState(arc4.Address())
        self.f_bps = GlobalState(UInt64(0))
        self.s_bps = GlobalState(UInt64(0))
        self.min_stake = GlobalState(UInt64(0))
        self.escrowed = GlobalState(UInt64(0))
        self.slots = BoxMap(arc4.UInt64, Slot, key_prefix="s")
        self.answers = BoxMap(Bytes, Answer, key_prefix="a")
        self.stakes = BoxMap(Bytes, Stake, key_prefix="k")

    # ------------------------------------------------------------------ setup

    @arc4.abimethod(create="require")
    def create(
        self,
        treasury: arc4.Address,
        f_bps: UInt64,
        s_bps: UInt64,
        min_stake: UInt64,
    ) -> None:
        assert f_bps > UInt64(0) and f_bps <= UInt64(BPS), "f_bps"
        assert s_bps <= UInt64(BPS), "s_bps"
        assert min_stake > UInt64(0), "min_stake"
        self.treasury.value = treasury
        self.f_bps.value = f_bps
        self.s_bps.value = s_bps
        self.min_stake.value = min_stake

    # ------------------------------------------------------------------ stake

    @arc4.abimethod
    def stake(
        self,
        pay: gtxn.PaymentTransaction,
        slot_id: arc4.UInt64,
        content_hash: arc4.DynamicBytes,
    ) -> None:
        assert pay.sender == Txn.sender, "pay sender"
        assert pay.receiver == Global.current_application_address, "pay receiver"
        assert pay.rekey_to == Global.zero_address, "rekey"
        assert pay.close_remainder_to == Global.zero_address, "close"
        assert pay.amount > UInt64(0), "amount"
        h = content_hash.native
        assert h.length == UInt64(32), "hash length"

        sid = slot_id.native
        amount = pay.amount
        skey = op.itob(sid) + Txn.sender.bytes
        akey = op.itob(sid) + h

        if akey in self.answers:
            ans = self.answers[akey].copy()
            assert not ans.settled.native, "hash displaced"
        else:
            ans = Answer(
                total=arc4.UInt64(0),
                acc=arc4.UInt64(0),
                settled=arc4.Bool(False),
                acc_at_settle=arc4.UInt64(0),
                settle_total=arc4.UInt64(0),
                refund_pool=arc4.UInt64(0),
            )

        # one stake box per (slot, address): it carries one hash at a time
        staked = amount
        debt = amount * ans.acc.native // UInt64(ACC)
        if skey in self.stakes:
            st = self.stakes[skey].copy()
            assert st.hash.native == h, "unstake first"
            staked += st.amount.native
            debt += st.debt.native
        self.stakes[skey] = Stake(
            hash=content_hash.copy(), amount=arc4.UInt64(staked), debt=arc4.UInt64(debt)
        )

        total_after = ans.total.native + amount
        ans.total = arc4.UInt64(total_after)
        self.answers[akey] = ans.copy()

        if slot_id not in self.slots:
            assert amount >= self.min_stake.value, "min stake"
            self.slots[slot_id] = Slot(
                top_hash=content_hash.copy(),
                top_total=arc4.UInt64(total_after),
                second_hash=arc4.DynamicBytes(),
                second_total=arc4.UInt64(0),
                epoch=arc4.UInt64(1),
            )
        else:
            slot = self.slots[slot_id].copy()
            if slot.top_total.native == UInt64(0):
                # empty slot: any accumulated hash can take it, if it clears the floor
                assert total_after >= self.min_stake.value, "min stake"
                slot.top_hash = content_hash.copy()
                slot.top_total = arc4.UInt64(total_after)
            elif slot.top_hash.native == h:
                slot.top_total = arc4.UInt64(slot.top_total.native + amount)
            elif total_after > slot.top_total.native:
                slot = self._displace(sid, slot, akey, content_hash, total_after)
            else:
                if total_after > slot.second_total.native:
                    slot.second_hash = content_hash.copy()
                    slot.second_total = arc4.UInt64(total_after)
            self.slots[slot_id] = slot.copy()

        self.escrowed.value += amount

    def _displace(
        self,
        sid: UInt64,
        slot: Slot,
        winner_key: Bytes,
        winner_hash: arc4.DynamicBytes,
        winner_total: UInt64,
    ) -> Slot:
        """The incumbent lost: freeze it with a refund pool, credit the winner, take the fee."""
        old_total = slot.top_total.native
        fee = old_total * self.f_bps.value // UInt64(BPS)
        reward = fee * self.s_bps.value // UInt64(BPS)
        to_treasury = fee - reward

        old_key = op.itob(sid) + slot.top_hash.native
        old = self.answers[old_key].copy()
        old.settled = arc4.Bool(True)
        old.acc_at_settle = old.acc
        old.settle_total = arc4.UInt64(old_total)
        old.refund_pool = arc4.UInt64(old_total - fee)
        self.answers[old_key] = old.copy()

        win = self.answers[winner_key].copy()
        win.acc = arc4.UInt64(win.acc.native + reward * UInt64(ACC) // winner_total)
        self.answers[winner_key] = win.copy()

        if to_treasury > UInt64(0):
            self.escrowed.value -= to_treasury
            self._send(self.treasury.value.native, to_treasury)

        slot.top_hash = winner_hash.copy()
        slot.top_total = arc4.UInt64(winner_total)
        slot.second_hash = arc4.DynamicBytes()
        slot.second_total = arc4.UInt64(0)
        slot.epoch = arc4.UInt64(slot.epoch.native + UInt64(1))
        return slot

    # ---------------------------------------------------------------- unstake

    @arc4.abimethod
    def unstake(self, slot_id: arc4.UInt64, amount: UInt64) -> None:
        sid = slot_id.native
        skey = op.itob(sid) + Txn.sender.bytes
        assert skey in self.stakes, "no stake"
        st = self.stakes[skey].copy()
        assert amount > UInt64(0) and amount <= st.amount.native, "amount"

        h = st.hash.native
        akey = op.itob(sid) + h
        ans = self.answers[akey].copy()
        slot = self.slots[slot_id].copy()
        on_top = slot.top_total.native > UInt64(0) and slot.top_hash.native == h

        if ans.settled.native:
            # already lost: the fee was taken at displacement, this is the refund pool only
            refund = amount * ans.refund_pool.native // ans.settle_total.native
            fee = UInt64(0)
            acc_now = ans.acc_at_settle.native
        elif on_top:
            # leaving the top costs f, whether or not anyone pushed you
            fee = amount * self.f_bps.value // UInt64(BPS)
            refund = amount - fee
            acc_now = ans.acc.native
        else:
            # never held the slot: whole stake back, no fee
            refund = amount
            fee = UInt64(0)
            acc_now = ans.acc.native

        owed = st.amount.native * acc_now // UInt64(ACC)
        claim = owed - st.debt.native if owed > st.debt.native else UInt64(0)

        self.escrowed.value -= refund + fee + claim
        self._check_solvency(refund + fee + claim)

        self.answers[akey] = Answer(
            total=arc4.UInt64(ans.total.native - amount),
            acc=ans.acc,
            settled=ans.settled,
            acc_at_settle=ans.acc_at_settle,
            settle_total=ans.settle_total,
            refund_pool=ans.refund_pool,
        )

        remaining = st.amount.native - amount
        if remaining == UInt64(0):
            del self.stakes[skey]
        else:
            # the accrued claim has just been paid in full, so the remainder starts clean
            self.stakes[skey] = Stake(
                hash=st.hash.copy(),
                amount=arc4.UInt64(remaining),
                debt=arc4.UInt64(remaining * acc_now // UInt64(ACC)),
            )

        if on_top:
            slot.top_total = arc4.UInt64(slot.top_total.native - amount)
            if slot.top_total.native == UInt64(0):
                # the top walked away: promote the best known challenger, or leave it empty
                if slot.second_total.native > UInt64(0):
                    slot.top_hash = slot.second_hash.copy()
                    slot.top_total = slot.second_total
                else:
                    slot.top_hash = arc4.DynamicBytes()
                    slot.top_total = arc4.UInt64(0)
                slot.second_hash = arc4.DynamicBytes()
                slot.second_total = arc4.UInt64(0)
                slot.epoch = arc4.UInt64(slot.epoch.native + UInt64(1))
            self.slots[slot_id] = slot.copy()

        self._send(Txn.sender, refund + claim)
        if fee > UInt64(0):
            self._send(self.treasury.value.native, fee)

    # ---------------------------------------------------------- read revenue

    @arc4.abimethod
    def reward(self, pay: gtxn.PaymentTransaction, slot_id: arc4.UInt64) -> None:
        """Credit read revenue to the answer that currently holds the slot.

        The edge batches paid reads and settles them here in one payment, so a 1,000 uALGO
        read never pays a chain fee. The credit raises the answer's accumulator and backers
        pull their share with `claim`; nobody is iterated over.
        """
        assert pay.sender == Txn.sender, "pay sender"
        assert pay.receiver == Global.current_application_address, "pay receiver"
        assert pay.rekey_to == Global.zero_address, "rekey"
        assert pay.close_remainder_to == Global.zero_address, "close"
        assert pay.amount > UInt64(0), "amount"
        assert slot_id in self.slots, "no slot"

        slot = self.slots[slot_id].copy()
        assert slot.top_total.native > UInt64(0), "slot empty"
        akey = op.itob(slot_id.native) + slot.top_hash.native
        ans = self.answers[akey].copy()
        assert not ans.settled.native, "answer settled"
        assert ans.total.native > UInt64(0), "no total"

        inc = pay.amount * UInt64(ACC) // ans.total.native
        # dust protection: a reward too small to move the accumulator is rejected so the
        # operator batches instead of silently losing it
        assert inc > UInt64(0), "reward too small"
        assert ans.acc.native + inc < UInt64(MAX_ACC), "acc cap"

        ans.acc = arc4.UInt64(ans.acc.native + inc)
        self.answers[akey] = ans.copy()
        self.escrowed.value += pay.amount

    @arc4.abimethod
    def claim(self, slot_id: arc4.UInt64) -> None:
        """Pay the sender the read revenue accrued on the answer they back.

        Principal is untouched; this only settles the reward side. A later unstake still
        returns the stake (with the top-exit fee if they hold the slot).
        """
        sid = slot_id.native
        skey = op.itob(sid) + Txn.sender.bytes
        assert skey in self.stakes, "no stake"
        st = self.stakes[skey].copy()
        akey = op.itob(sid) + st.hash.native
        ans = self.answers[akey].copy()
        acc_now = ans.acc_at_settle.native if ans.settled.native else ans.acc.native

        owed = st.amount.native * acc_now // UInt64(ACC)
        assert owed > st.debt.native, "nothing to claim"
        amount = owed - st.debt.native
        self.stakes[skey] = Stake(hash=st.hash.copy(), amount=st.amount, debt=arc4.UInt64(owed))
        self.escrowed.value -= amount
        self._check_solvency(amount)
        self._send(Txn.sender, amount)

    # ------------------------------------------------------------------ view

    @arc4.abimethod(readonly=True)
    def get_slot(self, slot_id: arc4.UInt64) -> Slot:
        return self.slots[slot_id]

    @arc4.abimethod(readonly=True)
    def get_answer(self, slot_id: arc4.UInt64, content_hash: arc4.DynamicBytes) -> Answer:
        return self.answers[op.itob(slot_id.native) + content_hash.native]

    @arc4.abimethod(readonly=True)
    def get_stake(self, slot_id: arc4.UInt64, who: arc4.Address) -> Stake:
        return self.stakes[op.itob(slot_id.native) + who.native.bytes]

    @arc4.abimethod(readonly=True)
    def escrow_total(self) -> UInt64:
        return self.escrowed.value

    # ----------------------------------------------------------------- utils

    def _check_solvency(self, total_out: UInt64) -> None:
        assert op.balance(Global.current_application_address) >= total_out + self.escrowed.value, "solvency"

    def _send(self, to: Account, amount: UInt64) -> None:
        if amount > UInt64(0):
            itxn.Payment(receiver=to, amount=amount, fee=UInt64(0)).submit()
