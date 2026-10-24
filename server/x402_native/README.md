# Native-ALGO x402 (local implementation)

`@x402/avm` implements the x402 `exact` scheme with **ASA transfers only**, so an agent
that holds nothing but ALGO cannot pay. This directory implements the same HTTP flow
with a native `pay` transaction instead.

Verified on algokit localnet: 7/7 cases pass (`e2e_localnet.py`).

    python server/x402_native/e2e_localnet.py          # against localnet:4001
    python server/x402_native/demo_server.py --pay-to <ADDRESS> --port 8851

## Flow

    GET /paid                          -> 402 + accepts[{scheme:exact, asset:"ALGO", payTo, maxAmountRequired, extra.paymentId}]
    GET /paid  PAYMENT-SIGNATURE: ...  -> 200 + PAYMENT-RESPONSE{success, transaction, confirmedRound}

The payment payload is a base64 JSON envelope whose `payload.txn` is a base64 msgpack
`SignedTransaction` of a native payment. The transaction's `note` carries the challenge's
`paymentId`, which binds the payment to the request and makes it single-use.

## Server-side checks (all before submit)

| check | why |
| --- | --- |
| `type == pay`, no group | a grouped tx could smuggle other effects |
| `receiver == payTo` | pays the advertised address only |
| `amt >= maxAmountRequired` | no underpayment |
| `fee <= 2 x min fee` | payer cannot be griefed into a huge fee |
| `note == paymentId` | binds to this challenge, blocks replay on another resource |
| challenge unused, unexpired, same resource | single use, time bound |
| `last_valid_round >= round`, `first_valid_round` fresh | no stale transactions |
| genesis hash matches | no cross-network replay |
| txid unseen | belt and braces on replay |

Settlement = submitting the signed transaction; the network is the signature check.
Every rejection returns 402 with a fresh challenge and an `error` string.

## Deviation from the spec (read this before advertising it)

The AVM spec requires `asset` to be an ASA id. This implementation sends `"asset": "ALGO"`,
which standard x402 clients will not parse. That makes it x402-shaped, not
x402-interoperable.

Recommended deployment: advertise **two** entries in `accepts` — the standard ASA/USDC
`exact` entry for foreign agents, and this native-ALGO entry for agents that hold only
ALGO. The facilitator then branches on the payload's asset. Keeping only the native entry
means every client must be ours, and third-party facilitators or discovery services will
not recognise the payment.

## Cost comparison (measured)

| path | txns per payment | payer needs | one-time cost for a fresh agent wallet |
| --- | --- | --- | --- |
| ASA/USDC exact | 1 (+1 opt-in once) | USDC + ALGO for fee | 1 txn + 0.1 ALGO MBR |
| native ALGO (this) | 1 | ALGO only | none |

The fee is 1,000 microALGO either way ($0.0001 at ALGO $0.1047). The saving is the ASA
opt-in and the USDC sourcing path, not the transaction fee.
