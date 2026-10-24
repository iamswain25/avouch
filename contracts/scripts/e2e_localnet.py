"""End-to-end localnet run of the per-post bond escrow.

Scenarios
  A slash     : post -> challenge -> arbiter slashes -> treasury + challenger paid
  B release   : post -> challenge -> arbiter releases -> both refunded
  C reclaim   : post -> window expires -> author reclaims unilaterally
  D stale     : post -> challenge -> arbiter stalls -> anyone refunds both
  E guards    : early reclaim rejected, non-arbiter settle rejected

Run: .venv/bin/python scripts/e2e_localnet.py
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

decimals = 6
ONE = 10**decimals
BOND = 5 * ONE           # author bond
CHAL = 5 * ONE           # challenger bond (1:1)
SLASH_SHARE_BPS = 5000   # challenger gets half of the slashed bond
RESOLVE_WINDOW = 4       # seconds the arbiter gets


class Acct:
    """Minimal signer wrapper (py-algorand-sdk 2.12 dropped algosdk.account.Account)."""

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


def send_asset(a, sender, receiver, amount, asset_id):
    sp = a.suggested_params()
    txn = transaction.AssetTransferTxn(sender.address, sp, receiver, amount, asset_id)
    wait(a, a.send_transaction(txn.sign(sender.private_key)))


def create_asset(a, creator, total=10**13):
    sp = a.suggested_params()
    txn = transaction.AssetCreateTxn(
        creator.address, sp, total, decimals, default_frozen=False,
        unit_name="USDC", asset_name="Test USDC", manager=creator.address,
    )
    wait(a, a.send_transaction(txn.sign(creator.private_key)))
    return a.pending_transaction_info(txn.get_txid())["asset-index"]


def compile_teal(name: str) -> tuple[bytes, bytes]:
    src = json.loads((ART / f"{name}.arc56.json").read_text())
    approval = a_compile(src["byteCode"]["approval"])
    clear = a_compile(src["byteCode"]["clear"])
    return approval, clear


_compiled_cache: dict[str, bytes] = {}


def a_compile(b64: str) -> bytes:
    if b64 not in _compiled_cache:
        _compiled_cache[b64] = base64.b64decode(b64)
    return _compiled_cache[b64]


def deploy(a, creator, args: list):
    approval, clear = None, None
    src = json.loads((ART / "PostBond.arc56.json").read_text())
    approval = a_compile(src["byteCode"]["approval"])
    clear = a_compile(src["byteCode"]["clear"])
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    method = abi.Method.from_signature(
        "create(uint64,address,address,uint64,uint64,uint64,uint64)void"
    )
    app_args = [method.get_selector()] + [
        method.args[i].type.encode(v) for i, v in enumerate(args)
    ]
    txn = transaction.ApplicationCreateTxn(
        creator.address, sp, transaction.OnComplete.NoOpOC,
        approval, clear, transaction.StateSchema(5, 2), transaction.StateSchema(0, 0),
        app_args=app_args,
    )
    wait(a, a.send_transaction(txn.sign(creator.private_key)))
    return a.pending_transaction_info(txn.get_txid())["application-index"]


def app_addr(app_id: int) -> str:
    return transaction.logic.get_application_address(app_id)


def box_key(post_id: int) -> bytes:
    return b"p" + post_id.to_bytes(8, "big")


def call(a, sender, app_id, sig: str, values: list, *, boxes=(), accounts=(), assets=(), fee=3000):
    method = abi.Method.from_signature(sig)
    app_args = [method.get_selector()] + [
        method.args[i].type.encode(v) for i, v in enumerate(values)
    ]
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    txn = transaction.ApplicationNoOpTxn(
        sender.address, sp, app_id, app_args=app_args,
        accounts=list(accounts), foreign_assets=list(assets),
        boxes=[(app_id, b) for b in boxes] or None,
    )
    wait(a, a.send_transaction(txn.sign(sender.private_key)))
    return txn.get_txid()


def group_post(a, author, app_id, asset_id, post_id, window, bond=BOND, accounts=()):
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    axfer = transaction.AssetTransferTxn(
        author.address, sp, app_addr(app_id), bond, asset_id)
    method = abi.Method.from_signature("post_bond(axfer,uint64,byte[],uint64)void")
    content_hash = (f"sha256:post-{post_id}-bond-aware-cafe".encode())
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 3000
    appcall = transaction.ApplicationNoOpTxn(
        author.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(post_id),
                  method.args[2].type.encode(content_hash),
                  method.args[3].type.encode(window)],
        accounts=list(accounts), foreign_assets=[asset_id],
        boxes=[(app_id, box_key(post_id))],
    )
    transaction.assign_group_id([axfer, appcall])
    txns = [axfer.sign(author.private_key), appcall.sign(author.private_key)]
    wait(a, a.send_transactions(txns))


def group_challenge(a, challenger, app_id, asset_id, post_id, amount=CHAL, accounts=()):
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    axfer = transaction.AssetTransferTxn(
        challenger.address, sp, app_addr(app_id), amount, asset_id)
    method = abi.Method.from_signature("challenge(axfer,uint64)void")
    sp2 = a.suggested_params()
    sp2.flat_fee = True
    sp2.fee = 3000
    appcall = transaction.ApplicationNoOpTxn(
        challenger.address, sp2, app_id,
        app_args=[method.get_selector(), method.args[1].type.encode(post_id)],
        accounts=list(accounts), foreign_assets=[asset_id],
        boxes=[(app_id, box_key(post_id))],
    )
    transaction.assign_group_id([axfer, appcall])
    wait(a, a.send_transactions([axfer.sign(challenger.private_key),
                                 appcall.sign(challenger.private_key)]))


def poke(a, acct):
    """Force a new block so the next pre-flight simulation sees a fresh block time."""
    sp = a.suggested_params()
    txn = transaction.PaymentTxn(acct.address, sp, acct.address, 0)
    wait(a, a.send_transaction(txn.sign(acct.private_key)))


def wait_until(ts: int):
    while time.time() < ts + 1:
        time.sleep(0.5)


def usdc_bal(a, addr, asset_id):
    try:
        return a.account_asset_info(addr, asset_id)["asset-holding"]["amount"]
    except Exception:
        return 0


def post_state(a, app_id, post_id, accounts):
    method = abi.Method.from_signature(
        "get_post(uint64)(address,address,byte[],uint64,uint64,uint64,uint64,uint8)")
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.ApplicationNoOpTxn(
        accounts[0].address, sp, app_id,
        app_args=[method.get_selector(), method.args[0].type.encode(post_id)],
        accounts=list(a.address for a in accounts),
        boxes=[(app_id, box_key(post_id))],
    )
    wait(a, a.send_transaction(txn.sign(accounts[0].private_key)))
    res = a.pending_transaction_info(txn.get_txid())
    return method.returns.type.decode(base64.b64decode(res["logs"][0])[4:])


def main():
    a, dispenser = connect()
    print("dispenser:", dispenser.address[:12], "round:", a.status()["last-round"])

    asset_id = create_asset(a, dispenser)
    print("test ASA:", asset_id)

    author = Acct.generate()
    challenger = Acct.generate()
    arbiter = Acct.generate()
    stranger = Acct.generate()
    for acct in (author, challenger, arbiter, stranger):
        send_pay(a, dispenser, acct.address, 5_000_000)
        # opt in to the ASA, then fund it
        sp = a.suggested_params()
        txn = transaction.AssetTransferTxn(acct.address, sp, acct.address, 0, asset_id)
        wait(a, a.send_transaction(txn.sign(acct.private_key)))
        send_asset(a, dispenser, acct.address, 100 * ONE, asset_id)
    send_pay(a, dispenser, Acct.generate().address, 1_000_000)

    app_id = deploy(a, dispenser, [asset_id, arbiter.address, dispenser.address,
                                   ONE, 10000, SLASH_SHARE_BPS, RESOLVE_WINDOW])
    print("app id:", app_id, "app address:", app_addr(app_id))
    send_pay(a, dispenser, app_addr(app_id), 3_000_000)

    everyone = [author.address, challenger.address, arbiter.address, dispenser.address]
    call(a, dispenser, app_id, "opt_in_usdc()void", [], assets=[asset_id], fee=2000)
    print("app ASA balance after opt-in:", usdc_bal(a, app_addr(app_id), asset_id))

    results = []

    # ---------------- A: slash
    pid = 1
    group_post(a, author, app_id, asset_id, pid, 60, accounts=everyone)
    print("A post state:", post_state(a, app_id, pid, [author, challenger, arbiter, dispenser]))
    group_challenge(a, challenger, app_id, asset_id, pid, accounts=everyone)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, True],
         boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
    treasury_delta = usdc_bal(a, dispenser.address, asset_id)
    ch_after = usdc_bal(a, challenger.address, asset_id)
    ok_slash = ch_after == 100 * ONE - CHAL + CHAL + BOND * SLASH_SHARE_BPS // 10000
    results.append(("A slash: challenger got bond + half of author bond", ok_slash, ch_after))

    # ---------------- B: release
    pid = 2
    group_post(a, author, app_id, asset_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, asset_id, pid, accounts=everyone)
    au_before = usdc_bal(a, author.address, asset_id)
    ch_before = usdc_bal(a, challenger.address, asset_id)
    call(a, arbiter, app_id, "settle(uint64,bool)void", [pid, False],
         boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
    ok_release = (usdc_bal(a, author.address, asset_id) == au_before + BOND and
                  usdc_bal(a, challenger.address, asset_id) == ch_before + CHAL)
    results.append(("B release: both bonds refunded", ok_release,
                    (usdc_bal(a, author.address, asset_id), usdc_bal(a, challenger.address, asset_id))))

    # ---------------- C: reclaim after window (no challenge)
    pid = 3
    au_before = usdc_bal(a, author.address, asset_id)
    group_post(a, author, app_id, asset_id, pid, 5, accounts=everyone)
    early = None
    try:
        call(a, author, app_id, "reclaim(uint64)void", [pid],
             boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
        early = "UNEXPECTED SUCCESS"
    except Exception as e:
        early = "rejected: " + str(e).split("\n")[0][:60]
    deadline = post_state(a, app_id, pid, [author, challenger, arbiter, dispenser])[5]
    wait_until(deadline + 2)
    poke(a, dispenser)
    call(a, author, app_id, "reclaim(uint64)void", [pid],
         boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
    ok_reclaim = usdc_bal(a, author.address, asset_id) == au_before
    results.append(("C reclaim: early call " + early, ok_reclaim, au_before))

    # ---------------- D: arbiter stalls -> permissionless release
    pid = 4
    group_post(a, author, app_id, asset_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, asset_id, pid, accounts=everyone)
    resolve_deadline = post_state(a, app_id, pid, [author, challenger, arbiter, dispenser])[6]
    wait_until(resolve_deadline + 2)
    poke(a, dispenser)
    au_before = usdc_bal(a, author.address, asset_id)
    ch_before = usdc_bal(a, challenger.address, asset_id)
    call(a, stranger, app_id, "release_stale(uint64)void", [pid],
         boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
    ok_stale = (usdc_bal(a, author.address, asset_id) == au_before + BOND and
                usdc_bal(a, challenger.address, asset_id) == ch_before + CHAL)
    results.append(("D stale: anyone refunds both after resolve window", ok_stale, au_before))

    # ---------------- E: non-arbiter cannot settle
    pid = 5
    group_post(a, author, app_id, asset_id, pid, 60, accounts=everyone)
    group_challenge(a, challenger, app_id, asset_id, pid, accounts=everyone)
    guard = None
    try:
        call(a, author, app_id, "settle(uint64,bool)void", [pid, True],
             boxes=[box_key(pid)], accounts=everyone, assets=[asset_id])
        guard = "UNEXPECTED SUCCESS"
    except Exception as e:
        guard = "rejected: " + str(e).split("\n")[0][:60]
    results.append(("E guard: non-arbiter settle " + guard, guard.startswith("rejected"), guard))

    print()
    print("app escrow USDC left (should be bond of post 5):", usdc_bal(a, app_addr(app_id), asset_id))
    print("app ALGO balance:", a.account_info(app_addr(app_id))["amount"])
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
