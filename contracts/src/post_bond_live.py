"""Living-bond escrow: a post can be challenged at any time while it stays bonded.

The v1 contract (post_bond_algo.py) gave every post a challenge window and returned the
bond when the window closed. That assumes a post stops being wrong. It does not: facts
change, and a page that was accurate in March can be misleading in June. So the window is
gone.

New rule, and its cost:

  - A post is challengeable for as long as its bond is locked. No deadline to miss.
  - The bond is therefore NOT returned on a timer. The author closes the post to get it
    back, and closing is a public act: the content stops being bonded and readers can see
    that. Removing the window moves the exit from time to choice.
  - Slashing is partial (forfeit_bps of the bond), so the post survives a ruling and stays
    challengeable afterwards. A full-slash design would end the post's life at the first
    ruling, which is the same finality the window gave us.

State machine

  post_bond  -> OPEN        (bond locked, no deadline)
  challenge  -> CHALLENGED  (challenger bond locked, resolve_deadline set)
  settle     -> OPEN        (partial forfeit, or challenger refunded)  [+rulings]
             -> CLOSED      (only if the remaining bond is below min_bond)
  close      -> CLOSED      (author only, OPEN only: bond refunded, post unforgeable)
  release_stale -> OPEN     (anyone, arbiter silent past resolve_deadline)

Invariant: challengeable == bonded. `close` is the only path that ends challengeability,
and it is visible on chain.
"""

from algopy import (
    ARC4Contract,
    Account,
    BoxMap,
    Global,
    GlobalState,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
)

OPEN = 0
CHALLENGED = 1
CLOSED = 2


class Post(arc4.Struct):
    author: arc4.Address
    challenger: arc4.Address
    content_hash: arc4.DynamicBytes
    bond: arc4.UInt64
    challenger_bond: arc4.UInt64
    resolve_deadline: arc4.UInt64
    status: arc4.UInt8
    rulings: arc4.UInt64
    forfeited: arc4.UInt64


class PostBondLive(ARC4Contract):
    def __init__(self) -> None:
        self.arbiter = GlobalState(arc4.Address())
        self.treasury = GlobalState(arc4.Address())
        self.min_bond = GlobalState(UInt64(0))
        self.challenger_bond_bps = GlobalState(UInt64(10000))
        self.challenger_share_bps = GlobalState(UInt64(5000))
        # share of the bond forfeited on a guilty ruling (partial: the post survives)
        self.forfeit_bps = GlobalState(UInt64(5000))
        self.resolve_window = GlobalState(UInt64(0))
        self.escrowed = GlobalState(UInt64(0))
        self.posts = BoxMap(arc4.UInt64, Post, key_prefix="p")

    # ---------------------------------------------------------------- setup

    @arc4.abimethod(create="require")
    def create(
        self,
        arbiter: arc4.Address,
        treasury: arc4.Address,
        min_bond: UInt64,
        challenger_bond_bps: UInt64,
        challenger_share_bps: UInt64,
        forfeit_bps: UInt64,
        resolve_window: UInt64,
    ) -> None:
        assert min_bond > UInt64(0), "min_bond"
        assert challenger_share_bps <= UInt64(10000), "share_bps"
        assert challenger_bond_bps <= UInt64(10000), "bond_bps"
        assert forfeit_bps > UInt64(0) and forfeit_bps <= UInt64(10000), "forfeit_bps"
        assert resolve_window > UInt64(0), "resolve_window"
        self.arbiter.value = arbiter
        self.treasury.value = treasury
        self.min_bond.value = min_bond
        self.challenger_bond_bps.value = challenger_bond_bps
        self.challenger_share_bps.value = challenger_share_bps
        self.forfeit_bps.value = forfeit_bps
        self.resolve_window.value = resolve_window

    # ---------------------------------------------------------------- posting

    @arc4.abimethod
    def post_bond(
        self,
        bond_txn: gtxn.PaymentTransaction,
        post_id: arc4.UInt64,
        content_hash: arc4.DynamicBytes,
    ) -> None:
        assert post_id not in self.posts, "post exists"
        assert bond_txn.sender == Txn.sender, "bond sender"
        assert bond_txn.receiver == Global.current_application_address, "bond receiver"
        assert bond_txn.rekey_to == Global.zero_address, "rekey"
        assert bond_txn.close_remainder_to == Global.zero_address, "close"
        assert bond_txn.amount >= self.min_bond.value, "bond too small"
        assert content_hash.length > UInt64(16), "content hash"

        self.escrowed.value += bond_txn.amount
        self.posts[post_id] = Post(
            author=arc4.Address.from_bytes(Txn.sender.bytes),
            challenger=arc4.Address(),
            content_hash=content_hash.copy(),
            bond=arc4.UInt64(bond_txn.amount),
            challenger_bond=arc4.UInt64(0),
            resolve_deadline=arc4.UInt64(0),
            status=arc4.UInt8(OPEN),
            rulings=arc4.UInt64(0),
            forfeited=arc4.UInt64(0),
        )

    @arc4.abimethod
    def challenge(
        self,
        challenge_txn: gtxn.PaymentTransaction,
        post_id: arc4.UInt64,
    ) -> None:
        post = self.posts[post_id].copy()
        # no time check: a bonded post is always challengeable
        assert post.status.native == UInt64(OPEN), "not open"
        assert challenge_txn.sender == Txn.sender, "challenge sender"
        assert challenge_txn.receiver == Global.current_application_address, "challenge receiver"
        assert challenge_txn.rekey_to == Global.zero_address, "rekey"

        required = post.bond.native * self.challenger_bond_bps.value // UInt64(10000)
        assert challenge_txn.amount >= required, "challenge bond too small"

        self.escrowed.value += challenge_txn.amount
        post.challenger = arc4.Address.from_bytes(Txn.sender.bytes)
        post.challenger_bond = arc4.UInt64(challenge_txn.amount)
        post.resolve_deadline = arc4.UInt64(Global.latest_timestamp + self.resolve_window.value)
        post.status = arc4.UInt8(CHALLENGED)
        self.posts[post_id] = post.copy()

    # --------------------------------------------------------------- settling

    @arc4.abimethod
    def settle(self, post_id: arc4.UInt64, guilty: arc4.Bool) -> None:
        post = self.posts[post_id].copy()
        assert Txn.sender == self.arbiter.value.native, "arbiter only"
        assert post.status.native == UInt64(CHALLENGED), "not challenged"

        challenger_bond = post.challenger_bond.native
        challenger = post.challenger.native
        post.challenger = arc4.Address()
        post.challenger_bond = arc4.UInt64(0)
        post.resolve_deadline = arc4.UInt64(0)
        post.rulings = arc4.UInt64(post.rulings.native + UInt64(1))

        if guilty.native:
            forfeit = post.bond.native * self.forfeit_bps.value // UInt64(10000)
            to_challenger = forfeit * self.challenger_share_bps.value // UInt64(10000)
            to_treasury = forfeit - to_challenger
            post.bond = arc4.UInt64(post.bond.native - forfeit)
            post.forfeited = arc4.UInt64(post.forfeited.native + forfeit)
            out = to_treasury + challenger_bond + to_challenger
            self.escrowed.value -= challenger_bond + forfeit
            self._check_solvency(out)
            self._send(self.treasury.value.native, to_treasury)
            self._send(challenger, challenger_bond + to_challenger)
        else:
            self.escrowed.value -= challenger_bond
            self._check_solvency(challenger_bond)
            self._send(challenger, challenger_bond)

        if post.bond.native >= self.min_bond.value:
            # the post stays alive and stays challengeable
            post.status = arc4.UInt8(OPEN)
            self.posts[post_id] = post.copy()
        else:
            # bond exhausted: close it and hand the remainder back
            remainder = post.bond.native
            self.escrowed.value -= remainder
            post.status = arc4.UInt8(CLOSED)
            self._check_solvency(remainder)
            self._send(post.author.native, remainder)
            del self.posts[post_id]

    @arc4.abimethod
    def close(self, post_id: arc4.UInt64) -> None:
        """Author ends the post: bond back, and the post stops being challengeable."""
        post = self.posts[post_id].copy()
        assert Txn.sender == post.author.native, "author only"
        assert post.status.native == UInt64(OPEN), "challenged"
        self.escrowed.value -= post.bond.native
        self._check_solvency(post.bond.native)
        self._send(post.author.native, post.bond.native)
        del self.posts[post_id]

    @arc4.abimethod
    def release_stale(self, post_id: arc4.UInt64) -> None:
        """Arbiter never ruled: challenger refunded, post returns to OPEN, still bonded."""
        post = self.posts[post_id].copy()
        assert post.status.native == UInt64(CHALLENGED), "not challenged"
        assert Global.latest_timestamp >= post.resolve_deadline.native, "arbiter still has time"
        challenger = post.challenger.native
        amount = post.challenger_bond.native
        post.challenger = arc4.Address()
        post.challenger_bond = arc4.UInt64(0)
        post.resolve_deadline = arc4.UInt64(0)
        post.rulings = arc4.UInt64(post.rulings.native + UInt64(1))
        post.status = arc4.UInt8(OPEN)
        self.escrowed.value -= amount
        self._check_solvency(amount)
        self._send(challenger, amount)
        self.posts[post_id] = post.copy()

    # ------------------------------------------------------------------ view

    @arc4.abimethod(readonly=True)
    def get_post(self, post_id: arc4.UInt64) -> Post:
        return self.posts[post_id]

    @arc4.abimethod(readonly=True)
    def escrow_total(self) -> UInt64:
        return self.escrowed.value

    # ----------------------------------------------------------------- utils

    def _check_solvency(self, total_out: UInt64) -> None:
        assert op.balance(Global.current_application_address) >= total_out + self.escrowed.value, "solvency"

    def _send(self, to: Account, amount: UInt64) -> None:
        if amount > UInt64(0):
            itxn.Payment(receiver=to, amount=amount, fee=UInt64(0)).submit()
