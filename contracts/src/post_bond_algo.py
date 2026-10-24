"""Per-post bond escrow in native ALGO (PuyaPy).

Why a second contract: the USDC version (post_bond.py) needs an ASA opt-in, an
asset id per network, foreign_assets on every call and inner asset transfers.
A native-ALGO bond removes all four. The bond and the box MBR then share one
balance, so this contract keeps an explicit `escrowed` total and refuses to pay
out more than the surplus above it — writer money can never be spent on MBR.

Lifecycle (identical to the USDC version)
  post_bond    : author locks bond            -> OPEN, capture_deadline
  challenge    : challenger locks its bond    -> CHALLENGED, resolve_deadline
  settle       : arbiter only (slash | release)
  reclaim      : author, after capture_deadline, only while OPEN
  release_stale: anyone, after resolve_deadline if the arbiter never ruled
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


class Post(arc4.Struct):
    author: arc4.Address
    challenger: arc4.Address
    content_hash: arc4.DynamicBytes
    bond: arc4.UInt64
    challenger_bond: arc4.UInt64
    capture_deadline: arc4.UInt64
    resolve_deadline: arc4.UInt64
    status: arc4.UInt8


class PostBondAlgo(ARC4Contract):
    def __init__(self) -> None:
        self.arbiter = GlobalState(arc4.Address())
        self.treasury = GlobalState(arc4.Address())
        self.min_bond = GlobalState(UInt64(0))
        self.challenger_bond_bps = GlobalState(UInt64(10000))
        self.challenger_share_bps = GlobalState(UInt64(5000))
        self.resolve_window = GlobalState(UInt64(0))
        # sum of every bond currently held; payouts may only touch the surplus above it
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
        resolve_window: UInt64,
    ) -> None:
        assert min_bond > UInt64(0), "min_bond"
        assert challenger_share_bps <= UInt64(10000), "share_bps"
        assert challenger_bond_bps <= UInt64(10000), "bond_bps"
        assert resolve_window > UInt64(0), "resolve_window"
        self.arbiter.value = arbiter
        self.treasury.value = treasury
        self.min_bond.value = min_bond
        self.challenger_bond_bps.value = challenger_bond_bps
        self.challenger_share_bps.value = challenger_share_bps
        self.resolve_window.value = resolve_window

    # ---------------------------------------------------------------- posting

    @arc4.abimethod
    def post_bond(
        self,
        bond_txn: gtxn.PaymentTransaction,
        post_id: arc4.UInt64,
        content_hash: arc4.DynamicBytes,
        window: UInt64,
    ) -> None:
        assert post_id not in self.posts, "post exists"
        assert window > UInt64(0), "window"
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
            capture_deadline=arc4.UInt64(Global.latest_timestamp + window),
            resolve_deadline=arc4.UInt64(0),
            status=arc4.UInt8(OPEN),
        )

    @arc4.abimethod
    def challenge(
        self,
        challenge_txn: gtxn.PaymentTransaction,
        post_id: arc4.UInt64,
    ) -> None:
        post = self.posts[post_id].copy()
        assert post.status.native == UInt64(OPEN), "not open"
        assert Global.latest_timestamp < post.capture_deadline.native, "window closed"
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
    def settle(self, post_id: arc4.UInt64, slash: arc4.Bool) -> None:
        post = self.posts[post_id].copy()
        assert Txn.sender == self.arbiter.value.native, "arbiter only"
        assert post.status.native == UInt64(CHALLENGED), "not challenged"

        held = post.bond.native + post.challenger_bond.native
        self.escrowed.value -= held

        if slash.native:
            to_challenger = post.bond.native * self.challenger_share_bps.value // UInt64(10000)
            to_treasury = post.bond.native - to_challenger
            self._check_solvency(to_treasury + post.challenger_bond.native + to_challenger)
            self._send(self.treasury.value.native, to_treasury)
            self._send(post.challenger.native, post.challenger_bond.native + to_challenger)
        else:
            self._check_solvency(held)
            self._send(post.author.native, post.bond.native)
            self._send(post.challenger.native, post.challenger_bond.native)

        del self.posts[post_id]

    @arc4.abimethod
    def reclaim(self, post_id: arc4.UInt64) -> None:
        post = self.posts[post_id].copy()
        assert Txn.sender == post.author.native, "author only"
        assert post.status.native == UInt64(OPEN), "challenged"
        assert Global.latest_timestamp >= post.capture_deadline.native, "window open"
        self.escrowed.value -= post.bond.native
        self._check_solvency(post.bond.native)
        self._send(post.author.native, post.bond.native)
        del self.posts[post_id]

    @arc4.abimethod
    def release_stale(self, post_id: arc4.UInt64) -> None:
        post = self.posts[post_id].copy()
        assert post.status.native == UInt64(CHALLENGED), "not challenged"
        assert Global.latest_timestamp >= post.resolve_deadline.native, "arbiter still has time"
        held = post.bond.native + post.challenger_bond.native
        self.escrowed.value -= held
        self._check_solvency(held)
        self._send(post.author.native, post.bond.native)
        self._send(post.challenger.native, post.challenger_bond.native)
        del self.posts[post_id]

    # ------------------------------------------------------------------ view

    @arc4.abimethod(readonly=True)
    def get_post(self, post_id: arc4.UInt64) -> Post:
        return self.posts[post_id]

    @arc4.abimethod(readonly=True)
    def escrow_total(self) -> UInt64:
        return self.escrowed.value

    # ----------------------------------------------------------------- utils

    def _check_solvency(self, total_out: UInt64) -> None:
        """Balance after these inner payments must still cover every remaining bond.

        Native ALGO means the box MBR shares the balance with the escrow, so this
        assertion is what keeps writer money from being spent on storage.
        """
        assert op.balance(Global.current_application_address) >= total_out + self.escrowed.value, "solvency"

    def _send(self, to: Account, amount: UInt64) -> None:
        if amount > UInt64(0):
            itxn.Payment(receiver=to, amount=amount, fee=UInt64(0)).submit()
