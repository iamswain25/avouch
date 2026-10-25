"""End-to-end test of the edge deployment (wrangler pages dev + localnet algod).

Covers the v4 slot auction surface: free index and meta, the paid read of the current
answer, stake and unstake plans, and (with --app-id) the plans actually executed against
the deployed contract on localnet, after which the index must report what the chain says.

Prereq:
  algokit localnet start
  # deploy contracts/src/slot_auction.py, fund the app account, note app id/address
  npx wrangler d1 execute avouch-board --local --file=schema.sql
  npx wrangler pages dev --port 8789 \
    --binding X402_SECRET=devsecret --binding PAY_TO=<treasury addr> \
    --binding ALGOD_URL=http://localhost:4001 --binding ALGOD_TOKEN=<token> \
    --binding APP_ID=<app id> --binding APP_ADDRESS=<app addr> --binding TREASURY=<treasury addr>

Run: .venv/bin/python scripts/e2e_worker_localnet.py [--base http://127.0.0.1:8789] [--app-id N]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from algosdk import account, encoding, mnemonic, transaction
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://localhost:4001"
KMD_URL = "http://localhost:4002"
TOKEN = "a" * 64
NETWORK = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe"


def enc_txn(signed) -> str:
    """payload.txn must be base64 of the msgpack bytes (normalized across SDK versions)."""
    v = encoding.msgpack_encode(signed)
    try:
        if base64.b64decode(v, validate=True)[:1] in (b"\x81", b"\x82", b"\x83", b"\x84"):
            return v
    except Exception:
        pass
    return base64.b64encode(v.encode("latin-1")).decode()


def connect(network: str = "localnet"):
    """localnet: read the dispenser key from KMD. testnet: read a funded mnemonic from a key file."""
    if network == "testnet":
        from algosdk import mnemonic
        import json as _json
        from pathlib import Path as _Path
        keyfile = _Path.home() / ".config" / "avouch" / "testnet.json"
        m = _json.loads(keyfile.read_text())["mnemonic"]
        sk = mnemonic.to_private_key(m)
        return algod.AlgodClient("", "https://testnet-api.algonode.cloud"), sk, account.address_from_private_key(sk)
    a = algod.AlgodClient(TOKEN, ALGOD_URL)
    k = KMDClient(TOKEN, KMD_URL)
    w = [x for x in k.list_wallets() if x["name"] == "unencrypted-default-wallet"][0]
    h = k.init_wallet_handle(w["id"], "")
    sk = k.export_key(h, "", k.list_keys(h)[0])
    return a, sk, account.address_from_private_key(sk)


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "avouch-e2e/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode()
            return r.status, dict(r.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:300]}
        return e.code, dict(e.headers), parsed


def pay_header(a, sk, accept):
    sender = account.address_from_private_key(sk)
    sp = a.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.PaymentTxn(sender, sp, accept["payTo"], int(accept["maxAmountRequired"]),
                                 note=accept["extra"]["paymentId"].encode())
    signed = txn.sign(sk)
    env = {"x402Version": 2, "scheme": accept["scheme"], "network": accept["network"],
           "payload": {"txn": enc_txn(signed)}}
    return base64.b64encode(json.dumps(env).encode()).decode(), signed.transaction.get_txid()


def exec_plan(a, sk, app_id, plan):
    """Sign and submit a plan returned by the API (pay + appl, or appl only)."""
    sender = account.address_from_private_key(sk)
    txns = []
    for leg in plan["atomic_group"]:
        sp = a.suggested_params()
        sp.flat_fee = True
        sp.fee = int(leg.get("fee_micro_algo") or 1000)
        if leg["txn"] == "pay":
            txns.append(transaction.PaymentTxn(sender, sp, leg["receiver"], int(leg["amount_micro_algo"]),
                                               note=str(leg.get("note") or "").encode()))
        else:
            args = [bytes.fromhex(x) for x in leg["app_args_hex"]]
            boxes = [(app_id, base64.b64decode(b)) for b in leg["foreign_boxes_b64"]]
            txns.append(transaction.ApplicationNoOpTxn(
                sender, sp, app_id, app_args=args, boxes=boxes,
                accounts=list(leg.get("foreign_accounts") or []),
            ))
    if len(txns) > 1:
        transaction.assign_group_id(txns)
    txid = a.send_transactions([t.sign(sk) for t in txns])
    transaction.wait_for_confirmation(a, txid, 10)
    return txid


class Mcp:
    def __init__(self, base):
        self.url = f"{base}/mcp"
        self.session = None
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        status, hdrs, body = http("POST", self.url, {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}, headers)
        self.session = self.session or hdrs.get("Mcp-Session-Id")
        return status, body

    def tool(self, name, args=None, payment=None):
        params = {"name": name, "arguments": args or {}}
        if payment:
            params["_meta"] = {"x402/payment": payment}
        return self.call("tools/call", params)


def teal_selector(signature):
    """ARC-4 selectors are sha512/256, which python's OpenSSL build here lacks; the compiled
    teal lists them, so the test reads the contract it is actually talking to."""
    teal = (Path(__file__).resolve().parent.parent / "contracts" / "src" / "artifacts" / "SlotAuction.approval.teal").read_text()
    for line in teal.splitlines():
        if "pushbytess" in line and f'method "{signature}"' in line:
            labels = line.split("//")[0].replace("pushbytess", "").split()
            names = line.split('method "')[1:]
            for label, name in zip(labels, names):
                if name.split('"')[0] == signature:
                    return label.replace("0x", "")
    raise SystemExit(f"selector for {signature} not found in the compiled teal")


def slot_row(idx, slot):
    return next((s for s in idx.get("slots", []) if s.get("slot_id") == slot), {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8789")
    ap.add_argument("--app-id", type=int, default=0)
    ap.add_argument("--network", default="localnet", choices=["localnet", "testnet"])
    args = ap.parse_args()
    base = args.base.rstrip("/")
    app_id = args.app_id

    a, dispenser_sk, dispenser = connect(args.network)
    # testnet ALGO is scarce (10 ALGO/day from the dispenser), so the two players persist
    # across runs in ~/.config/avouch/testnet-players.json and are only topped up when short
    players_file = Path.home() / ".config" / "avouch" / "testnet-players.json"
    if args.network == "testnet" and players_file.exists():
        pj = json.loads(players_file.read_text())
        payer_sk = mnemonic.to_private_key(pj["payer"])
        rival_sk = mnemonic.to_private_key(pj["rival"])
        payer = account.address_from_private_key(payer_sk)
        rival = account.address_from_private_key(rival_sk)
    else:
        payer_sk, payer = account.generate_account()
        rival_sk, rival = account.generate_account()
        if args.network == "testnet":
            players_file.parent.mkdir(parents=True, exist_ok=True)
            players_file.write_text(json.dumps({
                "payer": mnemonic.from_private_key(payer_sk),
                "rival": mnemonic.from_private_key(rival_sk),
            }, indent=1))
            players_file.chmod(0o600)

    if args.network == "testnet":
        # the daily dispenser limit is 10 ALGO, so keep chain amounts at the policy floor and
        # move only what each player is short of (stake + fees + the 0.1 ALGO min balance)
        stake_a, stake_b = 1_000_000, 1_200_000
        tr = json.loads((Path.home() / ".config" / "avouch" / "testnet-app.json").read_text())
        tr_sk = mnemonic.to_private_key(tr["treasury_mnemonic"])

        def missing(addr, need):
            try:
                return max(0, need - a.account_info(addr)["amount"])
            except Exception:
                return need

        payer_need = missing(payer, 1_150_000)
        rival_need = missing(rival, 1_350_000)
        if payer_need:
            sp = a.suggested_params()
            sp.flat_fee = True
            sp.fee = 1000
            fund = transaction.PaymentTxn(dispenser, sp, payer, payer_need)
            transaction.wait_for_confirmation(a, a.send_transaction(fund.sign(dispenser_sk)), 10)
        if rival_need:
            sp = a.suggested_params()
            sp.flat_fee = True
            sp.fee = 1000
            fund = transaction.PaymentTxn(account.address_from_private_key(tr_sk), sp, rival, rival_need)
            transaction.wait_for_confirmation(a, a.send_transaction(fund.sign(tr_sk)), 10)
        print(f"players funded from deployer/treasury: payer +{payer_need}, rival +{rival_need} uALGO")
    else:
        stake_a, stake_b = 2_000_000, 3_000_000
        for _sk, addr in ((payer_sk, payer), (rival_sk, rival)):
            sp = a.suggested_params()
            fund = transaction.PaymentTxn(dispenser, sp, addr, 20_000_000)
            transaction.wait_for_confirmation(a, a.send_transaction(fund.sign(dispenser_sk)), 10)

    results = []
    stamp = str(int(time.time()))
    slot = str(int(time.time()) % 100000)
    body_a = f"slot answer A {stamp}"
    hash_a = hashlib.sha256(body_a.encode()).hexdigest()
    body_b = f"slot answer B {stamp}"
    hash_b = hashlib.sha256(body_b.encode()).hexdigest()

    # 1 free meta
    st, _, meta = http("GET", f"{base}/api/meta")
    results.append(("1 GET /api/meta (free)", st == 200 and meta.get("min_stake_micro_algo") == 100000
                    and meta.get("policy_first_stake_micro_algo") == 1000000,
                    f"{st} app={meta.get('app_id')} floor={meta.get('min_stake_micro_algo')} policy={meta.get('policy_first_stake_micro_algo')}"))

    # 2 free index
    st, _, idx = http("GET", f"{base}/api/posts")
    results.append(("2 GET /api/posts (free)", st == 200 and "slots" in idx, f"{st} count={idx.get('count')} chain={idx.get('chain')}"))

    # 3 MCP handshake
    mcp = Mcp(base)
    st, body = mcp.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "edge-test", "version": "1"}})
    ok = st == 200 and body.get("result", {}).get("serverInfo", {}).get("name") == "avouch-board" and mcp.session
    results.append(("3 MCP initialize + session", bool(ok), f"{st} session={str(mcp.session)[:8]}"))
    mcp.call("notifications/initialized")

    # 4 tools/list: stake/unstake in, create/challenge out
    st, body = mcp.call("tools/list")
    names = [t["name"] for t in body.get("result", {}).get("tools", [])]
    results.append(("4 MCP tools/list: stake+unstake, no create/challenge",
                    st == 200 and {"stake", "unstake", "post_read", "board_index"} <= set(names)
                    and not ({"post_create", "post_challenge"} & set(names)), str(names)))

    # 5 stake plan rejected when the body does not match the hash
    st, _, bad = http("POST", f"{base}/api/posts/{slot}/stake",
                      {"sender": payer, "content_hash": "sha256:" + hash_a, "body": body_a + "tampered", "amount_micro_algo": 2_000_000})
    results.append(("5 stake plan rejects a body that does not match the hash", st == 400, f"{st} {bad.get('error')}"))

    # 6 first stake below the edge policy
    st, _, low = http("POST", f"{base}/api/posts/{slot}/stake",
                      {"sender": payer, "content_hash": "sha256:" + hash_a, "body": body_a, "amount_micro_algo": 100_000})
    results.append(("6 first stake below policy refused, both floors published",
                    st == 400 and low.get("policy_first_stake_micro_algo") == 1000000, f"{st} {low.get('error')}"))

    # 7 stake plan shape
    st, _, plan_a = http("POST", f"{base}/api/posts/{slot}/stake",
                         {"sender": payer, "content_hash": "sha256:" + hash_a, "body": body_a, "title": "A",
                          "amount_micro_algo": stake_a, "question": "which answer holds this slot"})
    if "atomic_group" not in plan_a:
        print("   stake plan response:", json.dumps(plan_a)[:400])
    legs = plan_a.get("atomic_group") or []
    app_leg = legs[1] if len(legs) == 2 else {}
    boxes = app_leg.get("foreign_boxes_b64") or []
    expected_sel = teal_selector("stake(pay,uint64,byte[])void")
    results.append(("7 stake plan: pay + appl, selector, 3 boxes",
                    st == 200 and len(legs) == 2 and app_leg.get("app_args_hex", [""])[0] == expected_sel and len(boxes) == 3,
                    f"{st} legs={len(legs)} sel={app_leg.get('app_args_hex', [''])[0]} boxes={len(boxes)}"))

    # 8 MCP stake tool returns the same plan shape
    st, mp = mcp.tool("stake", {"slot_id": slot, "sender": rival, "content_hash": "sha256:" + hash_b, "body": body_b, "amount_micro_algo": 1_000_000})
    plan_b_mcp = mp.get("result", {}).get("structuredContent") or {}
    results.append(("8 MCP stake plan", st == 200 and plan_b_mcp.get("slot_id") == slot and len(plan_b_mcp.get("atomic_group") or []) == 2,
                    f"{st} legs={len(plan_b_mcp.get('atomic_group') or [])}"))

    # 9 an empty slot has no body to sell
    st, _, empty_read = http("GET", f"{base}/api/posts/{slot}")
    results.append(("9 read of an empty slot is not sold", st in (409, 402), f"{st} {empty_read.get('error')}"))

    if app_id:
        # 10 the plan executes and the index reports what the chain says
        txid = exec_plan(a, payer_sk, app_id, plan_a)
        st, _, idx = http("GET", f"{base}/api/posts")
        row = slot_row(idx, slot)
        results.append(("10 on-chain stake: index reports chain state",
                        row.get("top_hash") == "sha256:" + hash_a and row.get("top_stake_micro_algo") == stake_a and row.get("epoch") == 1,
                        f"txid={txid[:10]} top={str(row.get('top_hash'))[:16]} stake={row.get('top_stake_micro_algo')} epoch={row.get('epoch')}"))

        # 11 paid read of the current answer
        st, _, unpaid_read = http("GET", f"{base}/api/posts/{slot}")
        acc = (unpaid_read.get("accepts") or [{}])[0]
        hdr, _ = pay_header(a, payer_sk, acc)
        st, _, read = http("GET", f"{base}/api/posts/{slot}", None, {"PAYMENT-SIGNATURE": hdr})
        ans = read.get("answer") or {}
        results.append(("11 paid read returns the current answer, hash verified",
                        st == 200 and ans.get("content_hash_verified") is True and ans.get("body") == body_a,
                        f"{st} epoch={(read.get('slot') or {}).get('epoch')} verified={ans.get('content_hash_verified')}"))

        # 12 replayed payment rejected
        st, _, replay = http("GET", f"{base}/api/posts/{slot}", None, {"PAYMENT-SIGNATURE": hdr})
        results.append(("12 replayed payment rejected", st == 402, f"{st} {replay.get('error')}"))

        # 13 a tampered payment (receiver swapped) is refused while the slot is live
        st, _, req = http("GET", f"{base}/api/posts/{slot}")
        acc_t = dict((req.get("accepts") or [{}])[0])
        hdr_t, _ = pay_header(a, payer_sk, acc_t)
        env_t = json.loads(base64.b64decode(hdr_t))
        signed_t = encoding.msgpack_decode(env_t["payload"]["txn"])
        signed_t.transaction.receiver = account.generate_account()[1]
        env_t["payload"]["txn"] = enc_txn(signed_t)
        hdr_t = base64.b64encode(json.dumps(env_t).encode()).decode()
        st, _, tampered = http("GET", f"{base}/api/posts/{slot}", None, {"PAYMENT-SIGNATURE": hdr_t})
        results.append(("13 tampered receiver rejected", st == 402, f"{st} {tampered.get('error')}"))

        # 13 bigger rival stake displaces: new top, epoch 2, contested, incumbent box listed
        st, _, plan_b = http("POST", f"{base}/api/posts/{slot}/stake",
                             {"sender": rival, "content_hash": "sha256:" + hash_b, "body": body_b, "title": "B", "amount_micro_algo": stake_b})
        incumbent_boxes = len(((plan_b.get("atomic_group") or [{}, {}])[1].get("foreign_boxes_b64")) or [])
        exec_plan(a, rival_sk, app_id, plan_b)
        st, _, idx = http("GET", f"{base}/api/posts")
        row = slot_row(idx, slot)
        results.append(("14 displacement: new top, epoch 2, replacements 1, incumbent box listed",
                        row.get("top_hash") == "sha256:" + hash_b and row.get("epoch") == 2 and row.get("replacements") == 1 and incumbent_boxes == 4,
                        f"top={str(row.get('top_hash'))[:16]} epoch={row.get('epoch')} replacements={row.get('replacements')} boxes={incumbent_boxes}"))

        # 14 the winner exits the top: one app call, the index follows the chain
        before = row.get("top_hash")
        st, _, up = http("POST", f"{base}/api/posts/{slot}/unstake",
                         {"sender": rival, "content_hash": "sha256:" + hash_b, "amount_micro_algo": stake_b})
        leg = (up.get("atomic_group") or [{}])[0]
        txid_u = exec_plan(a, rival_sk, app_id, up)
        st, _, idx2 = http("GET", f"{base}/api/posts")
        row2 = slot_row(idx2, slot)
        results.append(("15 unstake plan executes and the index follows",
                        st == 200 and leg.get("method") == "unstake(uint64,uint64)void" and row2.get("top_hash") != before,
                        f"txid={txid_u[:10]} top={str(row2.get('top_hash'))[:16]} epoch={row2.get('epoch')}"))
    else:
        results.append(("10-15 chain checks skipped (no --app-id)", True, "plan-level only"))


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
