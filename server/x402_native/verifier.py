"""x402 `exact` settlement for native ALGO (no ASA), implemented locally.

Why this exists: `@x402/avm` implements the Exact scheme with ASA transfers only,
so an agent that holds nothing but ALGO cannot pay through standard x402. This
module keeps the x402 HTTP shape (402 + PAYMENT-SIGNATURE + PAYMENT-RESPONSE) but
settles a native `pay` transaction instead of an ASA `axfer`.

Wire shape (what a client sees)

    402 {
          "x402Version": 2,
          "error": "payment required",
          "accepts": [{
            "scheme": "exact",
            "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe",
            "asset": "ALGO",              # deviation from the AVM spec, which requires an ASA id
            "maxAmountRequired": "1000",  # microALGO
            "resource": "/paid",
            "description": "...",
            "payTo": "<address>",
            "maxTimeoutSeconds": 120,
            "extra": {"paymentId": "<hex>", "minFeeMicroAlgo": 1000}
          }]
        }

    client -> PAYMENT-SIGNATURE: base64(json{"x402Version":2,"scheme":"exact",
                                             "network":..., "payload":{"txn":"<base64 msgpack SignedTransaction>"}})

    server -> PAYMENT-RESPONSE: base64(json{"success":true,"transaction":"<txid>","network":...})

Server-side rules (all enforced before submit)
  - single top-level transaction, type pay, no group
  - receiver == payTo, amount >= maxAmountRequired
  - fee <= 2x min fee (a payer cannot be griefed into paying a huge fee)
  - note == paymentId bytes: binds the payment to this challenge, so a captured
    transaction cannot be replayed against another request
  - first/last valid window sane (last_valid > current round, first_valid not stale)
  - paymentId and txid are single-use

Interoperability caveat: this is x402-shaped, not x402-interoperable. Standard
clients speak ASA-only payloads, so advertise this scheme as a second entry in
`accepts` and keep the ASA path for foreign agents.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass, field

import algosdk
from algosdk import encoding, transaction
from algosdk.v2client import algod

MIN_FEE = 1000
ALGOD_URL = os.environ.get("ALGOD_URL", "http://localhost:4001")
ALGOD_TOKEN = os.environ.get("ALGOD_TOKEN", "a" * 64)
NETWORK = os.environ.get("X402_NETWORK", "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe")


@dataclass
class Challenge:
    resource: str
    amount: int
    pay_to: str
    description: str = ""
    timeout_s: int = 120
    issued_at: float = field(default_factory=time.time)
    payment_id: bytes = b""
    used: bool = False


class NativeX402:
    def __init__(self, pay_to: str, algod_client: algod.AlgodClient | None = None):
        self.pay_to = pay_to
        self.algod = algod_client or algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)
        self.challenges: dict[str, Challenge] = {}
        self.settled_txids: set[str] = set()

    # ------------------------------------------------------------- challenge

    def new_challenge(self, resource: str, amount: int, description: str = "", timeout_s: int = 120) -> Challenge:
        pid = os.urandom(16)
        ch = Challenge(resource=resource, amount=amount, pay_to=self.pay_to,
                       description=description, timeout_s=timeout_s, payment_id=pid)
        self.challenges[pid.hex()] = ch
        return ch

    def payment_required(self, ch: Challenge) -> dict:
        return {
            "x402Version": 2,
            "error": "payment required",
            "accepts": [{
                "scheme": "exact",
                "network": NETWORK,
                "asset": "ALGO",
                "maxAmountRequired": str(ch.amount),
                "resource": ch.resource,
                "description": ch.description or f"paid access to {ch.resource}",
                "payTo": ch.pay_to,
                "maxTimeoutSeconds": ch.timeout_s,
                "extra": {"paymentId": ch.payment_id.hex(), "minFeeMicroAlgo": MIN_FEE},
            }],
        }

    # ---------------------------------------------------------------- verify

    def _decode_payload(self, header_b64: str) -> dict:
        try:
            raw = base64.b64decode(header_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"payment header is not base64: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"payment header is not json: {exc}") from exc

    def verify_and_settle(self, header_b64: str, expected_resource: str) -> dict:
        payload = self._decode_payload(header_b64)

        if payload.get("x402Version") != 2:
            raise ValueError("x402Version must be 2")
        if payload.get("scheme") != "exact":
            raise ValueError("scheme must be exact")
        if payload.get("network") != NETWORK:
            raise ValueError(f"network mismatch: {payload.get('network')}")

        body = payload.get("payload") or {}
        raw_txn = body.get("txn")
        if not raw_txn:
            raise ValueError("payload.txn missing")
        try:
            # documented format: base64 of msgpack bytes (msgpack_decode accepts both)
            signed = encoding.msgpack_decode(raw_txn)
        except Exception as exc:
            raise ValueError(f"payload.txn is not a signed transaction: {exc}") from exc

        txn = signed.transaction

        # structural checks
        if txn.type != "pay":
            raise ValueError(f"expected a pay transaction, got {txn.type}")
        if txn.group is not None:
            raise ValueError("grouped transactions are not accepted")
        if txn.receiver != self.pay_to:
            raise ValueError(f"receiver mismatch: {txn.receiver}")
        if txn.rekey_to is not None:
            raise ValueError("rekey_to is not allowed")
        if txn.close_remainder_to is not None:
            raise ValueError("close_remainder_to is not allowed")

        # bind to the challenge
        note = txn.note or b""
        pid_hex = note.decode("ascii", "ignore") if note else ""
        ch = self.challenges.get(pid_hex)
        if ch is None:
            raise ValueError("unknown or expired paymentId")
        if ch.used:
            raise ValueError("paymentId already used")
        if ch.resource != expected_resource:
            raise ValueError("paymentId was issued for a different resource")
        if abs(time.time() - ch.issued_at) > ch.timeout_s:
            raise ValueError("challenge expired")
        if txn.amt < ch.amount:
            raise ValueError(f"underpaid: {txn.amt} < {ch.amount}")
        if txn.fee is not None and txn.fee > 2 * MIN_FEE:
            raise ValueError("fee above the accepted cap")

        status = self.algod.status()
        current = status["last-round"]
        if txn.last_valid_round < current:
            raise ValueError("transaction expired")
        if current - txn.first_valid_round > 1000:
            raise ValueError("transaction first_valid too old")
        genesis = status.get("genesis-hash")
        if genesis and txn.genesis_hash != genesis:
            raise ValueError("wrong network: genesis hash mismatch")

        txid = txn.get_txid()
        if txid in self.settled_txids:
            raise ValueError("transaction already settled")

        # submit: the network is the signature check
        try:
            self.algod.send_raw_transaction(encoding.msgpack_encode(signed))
            transaction.wait_for_confirmation(self.algod, txid, 10)
        except Exception as exc:  # invalid signature, overspend, already in ledger
            raise ValueError(f"settlement failed: {exc}") from exc

        ch.used = True
        self.settled_txids.add(txid)
        info = self.algod.pending_transaction_info(txid)
        return {
            "success": True,
            "transaction": txid,
            "network": NETWORK,
            "confirmedRound": info.get("confirmed-round"),
            "payer": txn.sender,
            "amount": txn.amt,
        }

    # ---------------------------------------------------------------- client

    @staticmethod
    def encode_txn(signed_txn: transaction.SignedTransaction) -> str:
        """payload.txn is base64 of the msgpack bytes; normalize across SDK versions."""
        v = encoding.msgpack_encode(signed_txn)
        try:
            if base64.b64decode(v, validate=True)[:1] in (b"\x81", b"\x82", b"\x83", b"\x84"):
                return v
        except Exception:
            pass
        return base64.b64encode(v.encode("latin-1")).decode()

    @staticmethod
    def build_payment_header(signed_txn: transaction.SignedTransaction, network: str = NETWORK) -> str:
        inner = {
            "x402Version": 2,
            "scheme": "exact",
            "network": network,
            "payload": {"txn": NativeX402.encode_txn(signed_txn)},
        }
        return base64.b64encode(json.dumps(inner).encode()).decode()

    @staticmethod
    def build_payment_txn(private_key: str, algod_client: algod.AlgodClient, pay_to: str,
                          amount: int, payment_id_hex: str) -> transaction.SignedTransaction:
        sender = algosdk.account.address_from_private_key(private_key)
        sp = algod_client.suggested_params()
        sp.flat_fee = True
        sp.fee = MIN_FEE
        txn = transaction.PaymentTxn(sender, sp, pay_to, amount, note=payment_id_hex.encode())
        return txn.sign(private_key)
