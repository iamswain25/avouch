"""Deploy the v4 slot auction contract to a public network and wire it to the edge.

Network defaults to testnet via the public Nodely endpoints (no API token needed). The
deployer key comes from AVOUCH_TESTNET_MNEMONIC or ~/.config/avouch/testnet.json, which is
created by --new-account and never lives inside the repository.

    python scripts/deploy_slot_testnet.py --new-account          # prints the address to fund
    python scripts/deploy_slot_testnet.py --check-balance
    python scripts/deploy_slot_testnet.py --export               # deploy + print bindings

Run with the contracts virtualenv (it has algosdk):
    ~/workspace/algorand/avouch-contracts/.venv/bin/python scripts/deploy_slot_testnet.py --export
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
from pathlib import Path

from algosdk import abi, account, mnemonic, transaction
from algosdk.v2client import algod

ALGOD = "https://testnet-api.algonode.cloud"
GENESIS = "testnet-v1.0"
KEYFILE = Path.home() / ".config" / "avouch" / "testnet.json"
ART = Path(__file__).resolve().parent.parent / "contracts" / "src" / "artifacts"
ONE = 1_000_000


def load_key() -> str:
    env = os.environ.get("AVOUCH_TESTNET_MNEMONIC")
    if env:
        return env.strip()
    if KEYFILE.exists():
        return json.loads(KEYFILE.read_text())["mnemonic"]
    raise SystemExit(f"no deployer key: run --new-account or set AVOUCH_TESTNET_MNEMONIC (looked in {KEYFILE})")


def new_account() -> None:
    sk, addr = account.generate_account()
    KEYFILE.parent.mkdir(parents=True, exist_ok=True)
    KEYFILE.write_text(json.dumps({"address": addr, "mnemonic": mnemonic.from_private_key(sk)}, indent=1))
    KEYFILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"created {KEYFILE} (mode 600)")
    print(f"address to fund: {addr}")
    print("fund it at https://dispenser.testnet.aws.algodev.network/ or `algokit dispenser fund --receiver <addr>`")


def client() -> algod.AlgodClient:
    return algod.AlgodClient("", ALGOD)


def balance(a, addr: str) -> int:
    try:
        return a.account_info(addr)["amount"]
    except Exception:
        return 0


def send(a, sk: str, to: str, amount: int) -> str:
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.PaymentTxn(account.address_from_private_key(sk), sp, to, amount)
    txid = a.send_transaction(txn.sign(sk))
    transaction.wait_for_confirmation(a, txid, 10)
    return txid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new-account", action="store_true")
    ap.add_argument("--check-balance", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--fund-app", type=int, default=3, help="ALGO to send to the app account for box MBR")
    ap.add_argument("--fund-treasury", type=int, default=2, help="ALGO to seed the treasury")
    args = ap.parse_args()

    if args.new_account:
        new_account()
        return 0

    sk = mnemonic.to_private_key(load_key())
    deployer = account.address_from_private_key(sk)
    a = client()
    bal = balance(a, deployer)
    print(f"deployer {deployer} balance {bal / ONE:.3f} ALGO")
    if args.check_balance or not args.export:
        return 0
    if bal < 3 * ONE:
        raise SystemExit("not enough TestNet ALGO to create the app and boxes; fund the deployer first")

    treasury_sk, treasury = account.generate_account()

    src = json.loads((ART / "SlotAuction.arc56.json").read_text())
    approval = base64.b64decode(src["byteCode"]["approval"])
    clear = base64.b64decode(src["byteCode"]["clear"])
    method = abi.Method.from_signature("create(address,uint64,uint64,uint64)void")
    values = [treasury, 5000, 5000, 100_000]
    app_args = [method.get_selector()] + [method.args[i].type.encode(v) for i, v in enumerate(values)]

    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    create = transaction.ApplicationCreateTxn(
        deployer, sp, transaction.OnComplete.NoOpOC, approval, clear,
        transaction.StateSchema(4, 1), transaction.StateSchema(0, 0), app_args=app_args,
        extra_pages=1,   # reward/claim push the approval past one page
    )
    txid = a.send_transaction(create.sign(sk))
    transaction.wait_for_confirmation(a, txid, 10)
    app_id = a.pending_transaction_info(txid)["application-index"]
    app_address = transaction.logic.get_application_address(app_id)
    print(f"created app {app_id} at {app_address} (txid {txid})")

    if args.fund_app:
        print("funding app:", send(a, sk, app_address, args.fund_app * ONE)[:12])
    if args.fund_treasury:
        print("funding treasury:", send(a, sk, treasury, args.fund_treasury * ONE)[:12])

    out = {
        "app_id": app_id,
        "app_address": app_address,
        "treasury": treasury,
        "treasury_mnemonic": mnemonic.from_private_key(treasury_sk),
        "network": "testnet",
        "algod": ALGOD,
        "deployer": deployer,
    }
    (KEYFILE.parent / "testnet-app.json").write_text(json.dumps(out, indent=1))
    (KEYFILE.parent / "testnet-app.json").chmod(stat.S_IRUSR | stat.S_IWUSR)

    print()
    print(f"export APP_ID={app_id}")
    print(f'export APP_ADDRESS="{app_address}"')
    print(f'export TREASURY="{treasury}"')
    print(f'export ALGOD_URL="{ALGOD}"')
    print(f"(saved to {KEYFILE.parent / 'testnet-app.json'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
