// Board logic for the v4 slot auction. The chain owns the money; this file owns text,
// prices, and the two plans a client needs to move money:
//
//   POST /api/posts/:slot/stake     -> pay + appl group, args + box refs resolved
//   POST /api/posts/:slot/unstake   -> appl call, box refs resolved
//
// There is no bond status, no window, no challenge object, no arbiter and no ruling.
// A slot's answer is whichever hash has the most ALGO behind it (see contracts/src/
// slot_auction.py). Everything the board reports about money is read from the chain:
// if algod is not configured the API says so instead of guessing.

import {
  Algod,
  bytesToB64,
  bytesToHex,
  decodeAddress,
  makeChallenge,
  paymentRequired,
  verifyPayment,
} from "./algo.js";

export const READ_PRICE = 1000;      // microALGO per paid body read
export const MAINTAINER_BPS = 5000;  // share of a read that goes to the answer's backers
export const MAINTAINER_SHARE = Math.floor((READ_PRICE * MAINTAINER_BPS) / 10000);
// Prepaid credit: one deposit funds many reads. The deposit is a single payment, so the
// chain fee is amortised instead of paid twice per read. Custodial by design at this stage:
// the operator holds the balance in the pay-to account and settles or refunds it.
export const CREDIT_MIN_READS = 10;
export const CREDIT_MAX_READS = 1000;
export const CREDIT_TTL_DAYS = 90;
export const MIN_STAKE = 100_000;    // chain floor: 0.1 ALGO, covers the box MBR
export const POLICY = 1_000_000;     // edge policy: what the FIRST stake in a slot must clear
export const F_BPS = 5000;           // fee for leaving the top (contract default)
export const S_BPS = 5000;           // share of that fee paid to the side that took the slot
export const STAKE_SIG = "stake(pay,uint64,byte[])void";
export const UNSTAKE_SIG = "unstake(uint64,uint64)void";
// ARC-4 selectors are the first 4 bytes of SHA-512/256 of the signature. WebCrypto has no
// sha512/256, so the two are pinned to the compiled contract (contracts/src/artifacts/
// SlotAuction.approval.teal carries the same bytes) and the edge e2e asserts them.
export const STAKE_SELECTOR = "7787cadd";
export const UNSTAKE_SELECTOR = "85eaf93c";

const NOT_CONFIGURED = {
  error: "payments not configured",
  detail: "the operator has not set X402_SECRET, PAY_TO and ALGOD_URL yet",
  docs: "/en/agents",
};

const CHAIN_OFF = {
  error: "chain not configured",
  detail: "set APP_ID and ALGOD_URL so the board can read slot boxes; the chain is the only source of truth for stake",
  docs: "/en/agents",
};

export function config(env) {
  const configured = Boolean(env.X402_SECRET && env.PAY_TO && env.ALGOD_URL);
  return {
    configured,
    missing: ["X402_SECRET", "PAY_TO", "ALGOD_URL"].filter((k) => !env[k]).join(", "),
    secret: env.X402_SECRET,
    payTo: env.PAY_TO,
    network: env.X402_NETWORK || "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe",
    algod: new Algod(env.ALGOD_URL || "", env.ALGOD_TOKEN),
    indexer: env.INDEXER_URL || "",
    appId: Number(env.APP_ID || 0),
    appAddress: env.APP_ADDRESS || "",
    treasury: env.TREASURY || env.PAY_TO,
  };
}

export function paymentInstructions(cfg) {
  return {
    scheme: "exact-algo",
    standard: "NOT x402-compatible: x402 exact settles ASA/SPL/ERC-20 tokens only, this settles native ALGO",
    network: cfg.network,
    envelope: {
      x402Version: 2,
      scheme: "exact",
      network: cfg.network,
      payload: { txn: "<base64 msgpack SignedTransaction of a native pay>" },
    },
    transaction_rules: [
      "type must be pay and the transaction must not be grouped",
      "receiver must equal accepts[].payTo",
      "amount must be >= accepts[].maxAmountRequired",
      "note must be exactly accepts[].extra.paymentId (ASCII)",
      "fee must be <= 2 * 1000 microALGO",
      "rekey_to and close_remainder_to must be empty",
      "last_valid_round must not have passed at settlement time",
      "genesis hash must match the advertised network",
    ],
    error_codes: {
      underpaid: "amount below maxAmountRequired",
      "receiver mismatch": "receiver is not payTo",
      "unknown or expired paymentId": "paymentId missing from note or challenge expired",
      "paymentId already used": "on-chain replay: the transaction is already in the ledger",
      "transaction expired": "last_valid_round passed before settlement",
      "settlement failed": "network rejected the transaction (signature, funds)",
    },
    on_chain: {
      app_id: cfg.appId || null,
      stake: STAKE_SIG,
      unstake: UNSTAKE_SIG,
      fee_bps: F_BPS,
      winner_share_bps: S_BPS,
      note: "stake and unstake are not x402 resources; they return a transaction plan and the client signs it",
    },
    prices_micro_algo: {
      board_index: 0,
      post_read: READ_PRICE,
      min_stake: MIN_STAKE,
      policy_first_stake: POLICY,
      policy_note: "POLICY is an edge rule, not a contract constant: the first stake in a slot must clear it",
    },
    client_steps: [
      "call the resource without payment -> read accepts[0]",
      "build a native pay: payTo, maxAmountRequired, note = extra.paymentId",
      "retry with PAYMENT-SIGNATURE = base64(envelope)",
      "read PAYMENT-RESPONSE (HTTP) or structuredContent.receipt (MCP)",
    ],
  };
}

// ------------------------------------------------------------------ encoding

function u64be(n) {
  const out = new Uint8Array(8);
  let v = BigInt(n);
  for (let i = 7; i >= 0; i--) {
    out[i] = Number(v & 0xffn);
    v >>= 8n;
  }
  return out;
}

function concat(...parts) {
  const len = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(len);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function asciiBytes(s) {
  return new TextEncoder().encode(s);
}

export function slotKey(slotId) {
  return concat(asciiBytes("s"), u64be(slotId));
}

function answerKey(slotId, hashHex) {
  return concat(asciiBytes("a"), u64be(slotId), hexBytes(hashHex));
}

function stakeKey(slotId, address) {
  return concat(asciiBytes("k"), u64be(slotId), decodeAddress(address));
}

function hexBytes(hex) {
  const clean = String(hex || "").replace(/^sha256:/, "");
  if (!/^[0-9a-fA-F]{64}$/.test(clean)) return new Uint8Array(0);
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return out;
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return bytesToHex(new Uint8Array(buf));
}

// ---------------------------------------------------------------- chain read

// Slot box layout is ARC-4: 2-byte offsets, uint64s in the head, byte tails.
export function decodeSlotBox(v) {
  if (!v || v.length < 28) return null;
  const u64 = (o) => {
    let n = 0n;
    for (let i = 0; i < 8; i++) n = (n << 8n) | BigInt(v[o + i]);
    return Number(n);
  };
  const tail = (o) => {
    const len = (v[o] << 8) | v[o + 1];
    return bytesToHex(v.slice(o + 2, o + 2 + len));
  };
  const off0 = (v[0] << 8) | v[1];
  const off2 = (v[10] << 8) | v[11];
  return {
    top_hash: tail(off0),
    top_total: u64(2),
    second_hash: tail(off2),
    second_total: u64(12),
    epoch: u64(20),
  };
}

async function chainSlot(cfg, slotId) {
  if (!cfg.appId || !cfg.algod.url) return null;
  const box = await cfg.algod.box(cfg.appId, slotKey(slotId));
  if (!box) return { top_hash: "", top_total: 0, second_hash: "", second_total: 0, epoch: 0, empty: true };
  const decoded = decodeSlotBox(box);
  return decoded ? { ...decoded, empty: decoded.top_total === 0 } : null;
}

// ------------------------------------------------------------------ storage

async function listSlotRows(env, limit = 50) {
  if (!env.DB) return [];
  const { results } = await env.DB.prepare(
    "SELECT slot_id, question, created_at, updated_at FROM slots ORDER BY updated_at DESC LIMIT ?",
  ).bind(limit).all();
  return results || [];
}

async function answerRow(env, slotId, hashHex) {
  if (!env.DB) return null;
  return env.DB.prepare(
    "SELECT content_hash, title, body, author, created_at FROM answers WHERE slot_id = ? AND content_hash = ?",
  ).bind(String(slotId), `sha256:${String(hashHex).replace(/^sha256:/, "")}`).first();
}

async function knownAnswer(env, slotId) {
  if (!env.DB) return null;
  return env.DB.prepare(
    "SELECT content_hash FROM answers WHERE slot_id = ? ORDER BY created_at DESC LIMIT 1",
  ).bind(String(slotId)).first();
}

export async function indexSlots(env, cfg) {
  const rows = await listSlotRows(env);
  const chainOk = Boolean(cfg.appId && cfg.algod.url);
  const slots = await Promise.all(rows.map(async (row) => {
    const id = Number(row.slot_id);
    let state = null;
    try {
      state = chainOk ? await chainSlot(cfg, id) : null;
    } catch (err) {
      state = null;
    }
    const topHash = state && state.top_hash ? state.top_hash : "";
    const body = topHash ? await answerRow(env, id, topHash) : null;
    return {
      slot_id: row.slot_id,
      question: row.question || "",
      chain: state ? (state.empty ? "empty" : "open") : "unavailable",
      epoch: state ? state.epoch : null,
      replacements: state ? Math.max(0, state.epoch - 1) : null,
      top_hash: topHash ? `sha256:${topHash}` : null,
      top_stake_micro_algo: state ? state.top_total : null,
      second_stake_micro_algo: state ? state.second_total : null,
      contested: Boolean(state && state.second_total > 0),
      // contested = another hash currently holds stake; replacements = times the slot flipped
      top_body_stored: Boolean(body),
      top_title: body ? body.title : null,
      updated_at: row.updated_at,
    };
  }));
  return {
    slots,
    count: slots.length,
    read_price_micro_algo: READ_PRICE,
    chain: chainOk ? "algod" : "not configured",
    note: "stake, epoch and the current answer always come from the chain; this index only stores bodies",
  };
}

export async function boardMeta(env, cfg) {
  const rows = await listSlotRows(env, 50);
  let contested = 0;
  let open = 0;
  if (cfg.appId && cfg.algod.url) {
    await Promise.all(rows.map(async (row) => {
      try {
        const st = await chainSlot(cfg, Number(row.slot_id));
        if (st && !st.empty) open += 1;
        if (st && st.second_total > 0) contested += 1;
      } catch {
        /* one unreadable box must not fail the meta call */
      }
    }));
  }
  let readRevenue = { pending_micro_algo: 0, pending_reads: 0, last_settled_at: null, settlement_period: "24h, or 20,000 uALGO per slot, max 7 days" };
  if (env.DB) {
    try {
      const row = await env.DB.prepare(
        "SELECT COUNT(*) AS n, COALESCE(SUM(maintainer_micro_algo), 0) AS pending FROM reads WHERE batch_id IS NULL",
      ).first();
      const last = await env.DB.prepare("SELECT created_at FROM settlements ORDER BY created_at DESC LIMIT 1").first();
      readRevenue = {
        ...readRevenue,
        pending_reads: row ? row.n : 0,
        pending_micro_algo: row ? row.pending : 0,
        last_settled_at: last ? last.created_at : null,
      };
    } catch (err) {
      readRevenue.error = String(err.message || err);
    }
  }
  return {
    app_id: cfg.appId || null,
    network: cfg.network,
    read_revenue: readRevenue,
    read_price_micro_algo: READ_PRICE,
    credit: {
      price_per_read_micro_algo: READ_PRICE,
      min_reads: CREDIT_MIN_READS,
      max_reads: CREDIT_MAX_READS,
      ttl_days: CREDIT_TTL_DAYS,
      maintainer_share_per_read_micro_algo: MAINTAINER_SHARE,
      topup: "POST /api/credit/topup {reads}",
      custody: "operator holds the deposit until it is spent or refunded",
    },
    min_stake_micro_algo: MIN_STAKE,
    policy_first_stake_micro_algo: POLICY,
    fee_bps: F_BPS,
    winner_share_bps: S_BPS,
    slots_known: rows.length,
    slots_open: open,
    contested_share: rows.length ? contested / rows.length : 0,
    policy_note: "POLICY is an edge setting with a public change log, not a contract parameter",
  };
}

// ----------------------------------------------------------------- paid read

export async function readSlot(env, cfg, slotId, paymentHeader, creditHeader) {
  if (!cfg.configured) return { status: 503, body: NOT_CONFIGURED };
  const id = Number(slotId);
  if (!Number.isSafeInteger(id) || id < 0) return { status: 400, body: { error: `slot_id must be a uint64, got ${slotId}` } };
  if (!cfg.appId || !cfg.algod.url) return { status: 503, body: CHAIN_OFF };

  let state;
  try {
    state = await chainSlot(cfg, id);
  } catch (err) {
    return { status: 502, body: { error: `algod box read failed: ${String(err.message || err)}` } };
  }
  if (!state) return { status: 502, body: { error: "slot box unreadable" } };
  if (state.empty) return { status: 409, body: { error: `slot ${id} is empty`, slot_id: String(id), epoch: state.epoch } };

  const resource = `/api/posts/${id}`;
  if (!paymentHeader && !creditHeader) {
    return {
      status: 402,
      body: {
        ...(await paymentRequired({ resource, amount: READ_PRICE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret })),
        cheaper: {
          why: "a per-read payment pays a 1,000 uALGO chain fee on a 1,000 uALGO read",
          option: "prepay: POST /api/credit/topup {reads: 50}, then send X-CREDIT instead of PAYMENT-SIGNATURE",
          price_per_read_micro_algo: READ_PRICE,
        },
      },
    };
  }

  const row = await answerRow(env, id, state.top_hash);
  if (!row) {
    return {
      status: 404,
      body: {
        error: "body not stored for the current answer",
        slot_id: String(id),
        content_hash: `sha256:${state.top_hash}`,
        hint: "any address can POST the body to /api/posts/:slot/stake to store it",
      },
    };
  }

  // access is settled before the body is served, but only after we know the body exists,
  // so a credit is never spent on a 404
  let access;
  try {
    if (creditHeader) {
      const creditId = await readCreditToken(cfg.secret, creditHeader);
      const debit = await debitCredit(env, creditId, READ_PRICE);
      if (!debit) {
        const info = await env.DB.prepare("SELECT balance_micro_algo, expires_at FROM credits WHERE credit_id = ?").bind(creditId).first().catch(() => null);
        return {
          status: 402,
          body: {
            x402Version: 2,
            error: "credit exhausted or expired",
            resource,
            credit: {
              credit_id: creditId,
              balance_micro_algo: Number(info?.balance_micro_algo || 0),
              expires_at: info?.expires_at || null,
              topup: "POST /api/credit/topup {reads: N}",
            },
            accepts: [{
              scheme: "exact",
              network: cfg.network,
              asset: "ALGO",
              maxAmountRequired: String(READ_PRICE),
              resource,
              payTo: cfg.payTo,
              extra: (await paymentRequired({ resource, amount: READ_PRICE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret })).accepts[0].extra,
            }],
          },
        };
      }
      access = {
        success: true,
        scheme: "credit",
        credit_id: creditId,
        payer: debit.payer,
        transaction: `credit:${creditId}`,
        balance_micro_algo: debit.balance,
        reads_remaining: Math.floor(debit.balance / READ_PRICE),
      };
    } else {
      const receipt = await verifyPayment({ headerB64: paymentHeader, resource, secret: cfg.secret, payTo: cfg.payTo, algod: cfg.algod, network: cfg.network });
      if (!(await claimPayment(env, receipt.transaction, resource, receipt.payer, receipt.amount))) {
        return { status: 409, body: { error: "payment already used", transaction: receipt.transaction, hint: "one payment buys one read; prepay for many" } };
      }
      access = { scheme: "exact", ...receipt };
    }
  } catch (err) {
    if (creditHeader) return { status: 403, body: { error: `credit rejected: ${String(err.message || err)}` } };
    return {
      status: 402,
      body: await paymentRequired({ resource, amount: READ_PRICE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret, error: String(err.message || err) }),
    };
  }

  const verified = (await sha256Hex(row.body || "")) === state.top_hash;
  // the read is paid (or debited); record it for the settlement job. A storage failure must
  // not fail the read.
  if (env.DB) {
    try {
      await env.DB.prepare(
        "INSERT INTO reads (slot_id, content_hash, payer, txid, price_micro_algo, maintainer_micro_algo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
      ).bind(String(id), `sha256:${state.top_hash}`, access.payer || "", access.transaction, READ_PRICE, MAINTAINER_SHARE, Math.floor(Date.now() / 1000)).run();
    } catch (err) {
      console.error("read accounting failed", String(err));
    }
  }

  return {
    status: 200,
    body: {
      slot: {
        slot_id: String(id),
        epoch: state.epoch,
        top_hash: `sha256:${state.top_hash}`,
        top_stake_micro_algo: state.top_total,
        second_stake_micro_algo: state.second_total,
        contested: state.second_total > 0,
      },
      answer: { content_hash: row.content_hash, title: row.title, body: row.body, content_hash_verified: verified },
      receipt: access,
    },
  };
}



// A payment id is single-use. algod refuses a duplicate submission, but a client retrying after
// a slow confirmation must still be able to finish, so verification recovers "already in ledger"
// - which means the edge needs its own replay guard.
async function claimPayment(env, txid, resource, payer, amount) {
  if (!env.DB) return true;
  const res = await env.DB.prepare(
    "INSERT INTO payments (txid, resource, payer, amount, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(txid) DO NOTHING",
  ).bind(String(txid), String(resource), payer || "", Number(amount || 0), Math.floor(Date.now() / 1000)).run();
  return Boolean(res.meta && res.meta.changes === 1);
}

// ------------------------------------------------------------------ credit

async function creditMacKey(secret) {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

async function creditSignature(secret, creditId) {
  const key = await creditMacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`credit:${creditId}`));
  return bytesToHex(new Uint8Array(sig));
}

export async function creditToken(secret, creditId) {
  return `${creditId}.${(await creditSignature(secret, creditId)).slice(0, 32)}`;
}

export async function readCreditToken(secret, token) {
  const [creditId, sig] = String(token || "").split(".");
  if (!creditId || !sig) throw new Error("malformed credit token");
  const expected = (await creditSignature(secret, creditId)).slice(0, 32);
  if (expected.length !== sig.length) throw new Error("bad credit token");
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  if (diff !== 0) throw new Error("bad credit token");
  return creditId;
}

export function creditPrice(reads) {
  return Number(reads) * READ_PRICE;
}

export async function creditTopup(env, cfg, input = {}) {
  if (!cfg.configured) return { status: 503, body: NOT_CONFIGURED };
  if (!env.DB) return { status: 503, body: { error: "credits not configured", detail: "the D1 binding is missing" } };
  const reads = Number(input.reads ?? 50);
  if (!Number.isInteger(reads) || reads < CREDIT_MIN_READS || reads > CREDIT_MAX_READS) {
    return badRequest(`reads must be an integer between ${CREDIT_MIN_READS} and ${CREDIT_MAX_READS}`, {
      price_per_read_micro_algo: READ_PRICE,
      examples: { min: creditPrice(CREDIT_MIN_READS), max: creditPrice(CREDIT_MAX_READS) },
    });
  }
  const amount = creditPrice(reads);
  const creditId = crypto.randomUUID().replace(/-/g, "").slice(0, 24);
  const now = Math.floor(Date.now() / 1000);
  const expiresAt = now + CREDIT_TTL_DAYS * 86_400;
  await env.DB.prepare(
    "INSERT INTO credits (credit_id, deposited_micro_algo, balance_micro_algo, created_at, updated_at, expires_at) VALUES (?, ?, 0, ?, ?, ?)",
  ).bind(creditId, amount, now, now, expiresAt).run();
  // the note is a challenge bound to this credit, so a payment cannot be redeemed against
  // a different credit id
  const { paymentId } = await makeChallenge(cfg.secret, { resource: `/api/credit/redeem/${creditId}`, amount, ttlSeconds: 900 });
  return {
    status: 201,
    body: {
      credit_id: creditId,
      reads,
      amount_micro_algo: amount,
      price_per_read_micro_algo: READ_PRICE,
      fee_per_read_micro_algo: Math.ceil(1000 / reads),
      expires_at: expiresAt,
      payment: {
        scheme: "exact-algo",
        network: cfg.network,
        receiver: cfg.payTo,
        amount_micro_algo: amount,
        note: paymentId,
        note_ascii_only: true,
      },
      next: `send that payment, then POST /api/credit/redeem/${creditId} with PAYMENT-SIGNATURE; the response carries the credit token`,
      custody: "the deposit sits in the operator's pay-to account until it is spent or refunded",
    },
  };
}

export async function creditRedeem(env, cfg, creditId, paymentHeader) {
  if (!cfg.configured) return { status: 503, body: NOT_CONFIGURED };
  if (!env.DB) return { status: 503, body: { error: "credits not configured", detail: "the D1 binding is missing" } };
  if (!paymentHeader) return { status: 400, body: { error: "PAYMENT-SIGNATURE header missing" } };
  const row = await env.DB.prepare("SELECT credit_id, deposited_micro_algo, deposit_txid, expires_at FROM credits WHERE credit_id = ?").bind(String(creditId)).first();
  if (!row) return { status: 404, body: { error: `unknown credit ${creditId}`, hint: "POST /api/credit/topup first" } };
  if (row.deposit_txid) return { status: 409, body: { error: "credit already redeemed", deposit_txid: row.deposit_txid } };

  let receipt;
  try {
    receipt = await verifyPayment({
      headerB64: paymentHeader,
      resource: `/api/credit/redeem/${creditId}`,
      secret: cfg.secret,
      payTo: cfg.payTo,
      algod: cfg.algod,
      network: cfg.network,
    });
  } catch (err) {
    return { status: 402, body: { error: `deposit rejected: ${String(err.message || err)}` } };
  }

  if (!(await claimPayment(env, receipt.transaction, `/api/credit/redeem/${creditId}`, receipt.payer, receipt.amount))) {
    return { status: 409, body: { error: "payment already used", transaction: receipt.transaction } };
  }
  const now = Math.floor(Date.now() / 1000);
  const upd = await env.DB.prepare(
    "UPDATE credits SET balance_micro_algo = deposited_micro_algo, deposit_txid = ?, payer = ?, updated_at = ? WHERE credit_id = ? AND deposit_txid IS NULL",
  ).bind(receipt.transaction, receipt.payer || "", now, String(creditId)).run();
  if (!upd.meta || upd.meta.changes !== 1) return { status: 409, body: { error: "credit already redeemed" } };

  const balance = Number(row.deposited_micro_algo);
  return {
    status: 200,
    body: {
      credit_id: String(creditId),
      credit_token: await creditToken(cfg.secret, creditId),
      balance_micro_algo: balance,
      reads_remaining: Math.floor(balance / READ_PRICE),
      price_per_read_micro_algo: READ_PRICE,
      expires_at: Number(row.expires_at),
      deposit: receipt,
      usage: "send X-CREDIT: <credit_token> instead of PAYMENT-SIGNATURE on GET /api/posts/:slot",
    },
  };
}

export async function creditInfo(env, cfg, creditId, token) {
  if (!env.DB) return { status: 503, body: { error: "credits not configured" } };
  try {
    const id = await readCreditToken(cfg.secret, token || creditId);
    if (id !== String(creditId)) throw new Error("credit token does not match the id");
  } catch (err) {
    return { status: 403, body: { error: String(err.message || err) } };
  }
  const row = await env.DB.prepare(
    "SELECT credit_id, payer, deposited_micro_algo, balance_micro_algo, reads, created_at, updated_at, expires_at, refunded_txid FROM credits WHERE credit_id = ?",
  ).bind(String(creditId)).first();
  if (!row) return { status: 404, body: { error: `unknown credit ${creditId}` } };
  const now = Math.floor(Date.now() / 1000);
  return {
    status: 200,
    body: {
      ...row,
      reads_remaining: Math.floor(Number(row.balance_micro_algo) / READ_PRICE),
      expired: Number(row.expires_at) <= now,
      price_per_read_micro_algo: READ_PRICE,
    },
  };
}

async function debitCredit(env, creditId, price) {
  const now = Math.floor(Date.now() / 1000);
  const res = await env.DB.prepare(
    "UPDATE credits SET balance_micro_algo = balance_micro_algo - ?, reads = reads + 1, updated_at = ? WHERE credit_id = ? AND balance_micro_algo >= ? AND expires_at > ?",
  ).bind(price, now, creditId, price, now).run();
  if (!res.meta || res.meta.changes !== 1) return null;
  const row = await env.DB.prepare("SELECT payer, balance_micro_algo FROM credits WHERE credit_id = ?").bind(creditId).first();
  return { payer: row?.payer || "", balance: Number(row?.balance_micro_algo || 0) };
}

// ---------------------------------------------------------------- write plans

function badRequest(message, extra = {}) {
  return { status: 400, body: { error: message, ...extra } };
}

async function storeBody(env, slotId, input) {
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    "INSERT INTO slots (slot_id, question, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(slot_id) DO UPDATE SET updated_at = excluded.updated_at",
  ).bind(String(slotId), String(input.question || ""), now, now).run();
  await env.DB.prepare(
    "INSERT INTO answers (slot_id, content_hash, title, body, author, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(slot_id, content_hash) DO NOTHING",
  ).bind(String(slotId), `sha256:${String(input.content_hash).replace(/^sha256:/, "")}`, String(input.title || ""), String(input.body || ""), String(input.author || ""), now).run();
}

export async function stakePlan(env, cfg, slotId, input) {
  if (!cfg.appId) return { status: 503, body: CHAIN_OFF };
  const id = Number(slotId);
  if (!Number.isSafeInteger(id) || id < 0) return badRequest(`slot_id must be a uint64, got ${slotId}`);

  const amount = Number(input.amount_micro_algo || 0);
  const hashHex = String(input.content_hash || "").replace(/^sha256:/, "");
  const body = String(input.body || "");
  const sender = String(input.sender || "");

  if (!/^[0-9a-fA-F]{64}$/.test(hashHex)) return badRequest("content_hash must be sha256:<64 hex>");
  if (!sender) return badRequest("sender is required: the plan is built for one address");
  try {
    decodeAddress(sender);
  } catch {
    return badRequest("sender is not a valid Algorand address");
  }
  if (!Number.isSafeInteger(amount) || amount < MIN_STAKE) return badRequest(`amount below the chain floor ${MIN_STAKE}`, { min_stake_micro_algo: MIN_STAKE });
  if (!(await sha256Matches(hashHex, body))) return badRequest("body does not hash to content_hash", { content_hash: `sha256:${hashHex}` });

  let state = null;
  if (cfg.algod.url) {
    try {
      state = await chainSlot(cfg, id);
    } catch (err) {
      return { status: 502, body: { error: `algod box read failed: ${String(err.message || err)}` } };
    }
  }
  const firstStake = !state || state.empty;
  if (firstStake && amount < POLICY) {
    return badRequest(`the first stake in a slot must clear the edge policy ${POLICY}`, {
      policy_first_stake_micro_algo: POLICY,
      chain_floor_micro_algo: MIN_STAKE,
      note: "POLICY is an edge rule, not a contract constant",
    });
  }

  if (env.DB) await storeBody(env, id, { ...input, content_hash: hashHex });

  const appAddress = cfg.appAddress || appAddr(cfg.appId);
  const sel = STAKE_SELECTOR;
  const boxes = [slotKey(id), answerKey(id, hashHex), stakeKey(id, sender)];
  const accounts = cfg.treasury ? [cfg.treasury] : [];
  if (state && !state.empty && state.top_hash && state.top_hash !== hashHex) {
    // the incumbent's answer box is written by the contract on displacement
    const incumbent = answerKey(id, state.top_hash);
    if (!boxes.some((b) => bytesToHex(b) === bytesToHex(incumbent))) boxes.push(incumbent);
  }

  return {
    status: 200,
    body: {
      slot_id: String(id),
      current: state ? { epoch: state.epoch, top_hash: state.top_hash ? `sha256:${state.top_hash}` : null, top_stake_micro_algo: state.top_total, second_stake_micro_algo: state.second_total } : null,
      first_stake: firstStake,
      displaces: Boolean(state && !state.empty && state.top_total > 0 && state.top_hash !== hashHex),
      atomic_group: [
        { index: 0, txn: "pay", sender, receiver: appAddress, amount_micro_algo: amount, note: `avouch stake slot ${id}` },
        {
          index: 1,
          txn: "appl",
          sender,
          app_id: cfg.appId,
          method: STAKE_SIG,
          app_args_hex: [sel, bytesToHex(u64be(id)), `0020${hashHex}`], // byte[] carries a 2-byte length
          foreign_boxes_b64: boxes.map((b) => bytesToB64(b)),
          foreign_accounts: accounts,
          fee_micro_algo: 2000,
        },
      ],
      rules: [
        "both transactions in one atomic group, pay first",
        "the app call is signed by the same address as the pay",
        "include every foreign box listed; the app rejects an unlisted box",
        "a stake that can displace must also list the incumbent's answer box",
      ],
      effects: {
        fee_on_displacement_bps: F_BPS,
        winner_share_bps: S_BPS,
        note: "if this stake makes the hash the largest, the incumbent is frozen with a (10000-f)/10000 refund pool and the treasury takes the rest of the fee",
      },
    },
  };
}

export async function unstakePlan(env, cfg, slotId, input) {
  if (!cfg.appId) return { status: 503, body: CHAIN_OFF };
  const id = Number(slotId);
  if (!Number.isSafeInteger(id) || id < 0) return badRequest(`slot_id must be a uint64, got ${slotId}`);
  const amount = Number(input.amount_micro_algo || 0);
  const hashHex = String(input.content_hash || "").replace(/^sha256:/, "");
  const sender = String(input.sender || "");
  if (!sender) return badRequest("sender is required: the plan is built for one address");
  try {
    decodeAddress(sender);
  } catch {
    return badRequest("sender is not a valid Algorand address");
  }
  if (!/^[0-9a-fA-F]{64}$/.test(hashHex)) return badRequest("content_hash must be the hash you staked on (sha256:<64 hex>)");
  if (!Number.isSafeInteger(amount) || amount <= 0) return badRequest("amount_micro_algo must be positive");

  const sel = UNSTAKE_SELECTOR;
  const boxes = [slotKey(id), answerKey(id, hashHex), stakeKey(id, sender)];
  return {
    status: 200,
    body: {
      slot_id: String(id),
      atomic_group: [
        {
          index: 0,
          txn: "appl",
          sender,
          app_id: cfg.appId,
          method: UNSTAKE_SIG,
          app_args_hex: [sel, bytesToHex(u64be(id)), bytesToHex(u64be(amount))],
          foreign_boxes_b64: boxes.map((b) => bytesToB64(b)),
          foreign_accounts: cfg.treasury ? [cfg.treasury] : [],
          fee_micro_algo: 3000,
        },
      ],
      rules: [
        "no payment leg: the app pays you from the escrow it already holds",
        "include every foreign box listed, plus the treasury account",
      ],
      effects: {
        leaving_the_top_bps: F_BPS,
        note: "withdrawing from the current answer costs the same as being displaced; withdrawing from a losing answer returns its refund pool in full",
      },
    },
  };
}

async function sha256Matches(hashHex, body) {
  return (await sha256Hex(body)) === hashHex.toLowerCase();
}

function appAddr(appId) {
  // Algorand app address = base32(sha512/256("appID" + uint64be(appId)) + "Program"))
  // Computing it needs sha512/256, which crypto.subtle does not expose. The plan returns
  // the app id; the client derives the address with its SDK. We surface a hint instead.
  return `<app ${appId} address: derive with algosdk getApplicationAddress(${appId})>`;
}
