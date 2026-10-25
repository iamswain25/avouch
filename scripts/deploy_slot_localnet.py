"""Deploy the v4 slot auction contract to localnet and fund its account for box MBR.

Prints a shell fragment so the edge can be started against it:

    eval "$(.venv/bin/python scripts/deploy_slot_localnet.py --export)"

The outputs are APP_ID, APP_ADDRESS and TREASURY (the treasury used by the contract).

Run: .venv/bin/python scripts/deploy_slot_localnet.py [--parameters f=5000,s=5000,floor=100000]
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from algosdk import abi, account, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
ART = Path(__file__).resolve().parent.parent / "contracts" / "src" / "artifacts"

ONE = 1_000_000


def connect():
    a = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return a, sk, account.address_from_private_key(sk)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="print shell export lines")
    ap.add_argument("--fund", type=int, default=10, help="ALGO to send to the app account for box MBR")
    args = ap.parse_args()

    a, creator_sk, creator = connect()
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
    txn = transaction.ApplicationCreateTxn(
        creator, sp, transaction.OnComplete.NoOpOC, approval, clear,
        transaction.StateSchema(4, 1), transaction.StateSchema(0, 0), app_args=app_args,
        extra_pages=1,   # reward/claim push the approval past one page
    )
    transaction.wait_for_confirmation(a, a.send_transaction(txn.sign(creator_sk)), 10)
    app_id = a.pending_transaction_info(txn.get_txid())["application-index"]
    app_address = transaction.logic.get_application_address(app_id)

    if args.fund:
        sp = a.suggested_params()
        pay = transaction.PaymentTxn(creator, sp, app_address, args.fund * ONE)
        transaction.wait_for_confirmation(a, a.send_transaction(pay.sign(creator_sk)), 10)
        sp = a.suggested_params()
        seed = transaction.PaymentTxn(creator, sp, treasury, ONE)
        transaction.wait_for_confirmation(a, a.send_transaction(seed.sign(creator_sk)), 10)

    if args.export:
        print(f"export APP_ID={app_id}")
        print(f'export APP_ADDRESS="{app_address}"')
        print(f'export TREASURY="{treasury}"')
    else:
        print(json.dumps({"app_id": app_id, "app_address": app_address, "treasury": treasury}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
