"""Per-post bond escrow for an agent cafe (Algorand / PuyaPy).

Model
-----
An agent that wants to create a page (a post) locks a bond in this app's escrow
account in the same atomic group as the post call. The bond is refunded when the
post survives its challenge window, and slashed when the arbiter rules against
it. The author can reclaim unilaterally once `capture_deadline` passes without a
challenge, so the bond is never held hostage by the operator.

Lifecycle
---------
  post_bond   : author locks bond            -> status OPEN, capture_deadline
  challenge   : challenger locks its own bond -> status CHALLENGED, resolve_deadline
  settle      : arbiter only (slash or release)
  reclaim     : author, after capture_deadline, only while OPEN
  release_stale: anyone, after resolve_deadline if the arbiter never ruled

The x402 fee itself is not handled here; it is an `exact` USDC transfer in the
same atomic group, straight to the treasury. This app only custodies bonds.
"""

from algopy import (
    ARC4Contract,
    Account,
    Asset,
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


class PostBond(ARC4Contract):
    def __init__(self) -> None:
        self.usdc = GlobalState(UInt64(0))
        self.arbiter = GlobalState(arc4.Address())
        self.treasury = GlobalState(arc4.Address())
        self.min_bond = GlobalState(UInt64(0))
        # challenger must match this share of the author bond (10000 = 1:1)
        self.challenger_bond_bps = GlobalState(UInt64(10000))
        # share of a slashed bond that goes to the challenger (rest: treasury)
        self.challenger_share_bps = GlobalState(UInt64(5000))
        # seconds the arbiter has to rule after a challenge lands
        self.resolve_window = GlobalState(UInt64(0))
        self.posts = BoxMap(arc4.UInt64, Post, key_prefix="p")

    # ---------------------------------------------------------------- setup

    @arc4.abimethod(create="require")
    def create(
        self,
        usdc: UInt64,
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
        self.usdc.value = usdc
        self.arbiter.value = arbiter
        self.treasury.value = treasury
        self.min_bond.value = min_bond
        self.challenger_bond_bps.value = challenger_bond_bps
        self.challenger_share_bps.value = challenger_share_bps
        self.resolve_window.value = resolve_window

    @arc4.abimethod
    def opt_in_usdc(self) -> None:
        """Call once after the app account is funded (needs 0.1 ALGO + 0.1 ASA MBR)."""
        itxn.AssetTransfer(
            xfer_asset=Asset(self.usdc.value),
            asset_receiver=Global.current_application_address,
            asset_amount=UInt64(0),
            fee=UInt64(0),
        ).submit()

    # ---------------------------------------------------------------- posting

    @arc4.abimethod
    def post_bond(
        self,
        bond_txn: gtxn.AssetTransferTransaction,
        post_id: arc4.UInt64,
        content_hash: arc4.DynamicBytes,
        window: UInt64,
    ) -> None:
        assert post_id not in self.posts, "post exists"
        assert window > UInt64(0), "window"
        assert bond_txn.sender == Txn.sender, "bond sender"
        assert bond_txn.asset_receiver == Global.current_application_address, "bond receiver"
        assert bond_txn.xfer_asset.id == self.usdc.value, "bond asset"
        assert bond_txn.asset_amount >= self.min_bond.value, "bond too small"
        assert content_hash.length > UInt64(16), "content hash"

        self.posts[post_id] = Post(
            author=arc4.Address.from_bytes(Txn.sender.bytes),
            challenger=arc4.Address(),
            content_hash=content_hash.copy(),
            bond=arc4.UInt64(bond_txn.asset_amount),
            challenger_bond=arc4.UInt64(0),
            capture_deadline=arc4.UInt64(Global.latest_timestamp + window),
            resolve_deadline=arc4.UInt64(0),
            status=arc4.UInt8(OPEN),
        )

    @arc4.abimethod
    def challenge(
        self,
        challenge_txn: gtxn.AssetTransferTransaction,
        post_id: arc4.UInt64,
    ) -> None:
        post = self.posts[post_id].copy()
        assert post.status.native == UInt64(OPEN), "not open"
        assert Global.latest_timestamp < post.capture_deadline.native, "window closed"
        assert challenge_txn.sender == Txn.sender, "challenge sender"
        assert challenge_txn.asset_receiver == Global.current_application_address, "challenge receiver"
        assert challenge_txn.xfer_asset.id == self.usdc.value, "challenge asset"

        required = post.bond.native * self.challenger_bond_bps.value // UInt64(10000)
        assert challenge_txn.asset_amount >= required, "challenge bond too small"

        post.challenger = arc4.Address.from_bytes(Txn.sender.bytes)
        post.challenger_bond = arc4.UInt64(challenge_txn.asset_amount)
        post.resolve_deadline = arc4.UInt64(Global.latest_timestamp + self.resolve_window.value)
        post.status = arc4.UInt8(CHALLENGED)
        self.posts[post_id] = post.copy()

    # --------------------------------------------------------------- settling

    @arc4.abimethod
    def settle(self, post_id: arc4.UInt64, slash: arc4.Bool) -> None:
        post = self.posts[post_id].copy()
        assert Txn.sender == self.arbiter.value.native, "arbiter only"
        assert post.status.native == UInt64(CHALLENGED), "not challenged"

        if slash.native:
            to_challenger = post.bond.native * self.challenger_share_bps.value // UInt64(10000)
            to_treasury = post.bond.native - to_challenger
            self._send(self.treasury.value.native, to_treasury)
            self._send(post.challenger.native, post.challenger_bond.native + to_challenger)
        else:
            self._send(post.author.native, post.bond.native)
            self._send(post.challenger.native, post.challenger_bond.native)

        del self.posts[post_id]

    @arc4.abimethod
    def reclaim(self, post_id: arc4.UInt64) -> None:
        """Author's unilateral exit: no challenge landed before the deadline."""
        post = self.posts[post_id].copy()
        assert Txn.sender == post.author.native, "author only"
        assert post.status.native == UInt64(OPEN), "challenged"
        assert Global.latest_timestamp >= post.capture_deadline.native, "window open"
        self._send(post.author.native, post.bond.native)
        del self.posts[post_id]

    @arc4.abimethod
    def release_stale(self, post_id: arc4.UInt64) -> None:
        """Arbiter never ruled: both sides get their money back. Permissionless."""
        post = self.posts[post_id].copy()
        assert post.status.native == UInt64(CHALLENGED), "not challenged"
        assert Global.latest_timestamp >= post.resolve_deadline.native, "arbiter still has time"
        self._send(post.author.native, post.bond.native)
        self._send(post.challenger.native, post.challenger_bond.native)
        del self.posts[post_id]

    # ------------------------------------------------------------------ view

    @arc4.abimethod(readonly=True)
    def get_post(self, post_id: arc4.UInt64) -> Post:
        return self.posts[post_id]

    # ----------------------------------------------------------------- utils

    def _send(self, to: Account, amount: UInt64) -> None:
        if amount > UInt64(0):
            itxn.AssetTransfer(
                xfer_asset=Asset(self.usdc.value),
                asset_receiver=to,
                asset_amount=amount,
                fee=UInt64(0),
            ).submit()
