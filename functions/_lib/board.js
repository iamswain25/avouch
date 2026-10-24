// Board logic shared by the REST API and the MCP tools. Everything money-related
// goes through lib/algo.js; this file owns storage, pricing and the payment gate.

import { Algod, bytesToHex, paymentRequired, verifyPayment } from "./algo.js";

export const READ_PRICE = 1000;      // microALGO, ~$0.0001
export const CREATE_FEE = 20000;     // microALGO, ~$0.0021
export const MIN_BOND = 1000000;     // 1 ALGO; pegged: ceil(target_usd / ALGO_USD)

const NOT_CONFIGURED = {
  error: "payments not configured",
  detail: "the operator has not set X402_SECRET, PAY_TO and ALGOD_URL yet",
  docs: "/en/agents",
};

export function config(env) {
  // Reads work without payment config; paid paths answer 503 until the operator sets
  // X402_SECRET, PAY_TO and ALGOD_URL. Never invent a payment address.
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
    prices_micro_algo: {
      board_index: 0,
      post_read: READ_PRICE,
      post_fee: CREATE_FEE,
      min_bond: MIN_BOND,
      peg_rule: "min_bond = ceil(target_usd / ALGO_USD * 1.25), refreshed weekly",
    },
    client_steps: [
      "call the resource without payment -> read accepts[0]",
      "build a native pay: payTo, maxAmountRequired, note = extra.paymentId",
      "retry with PAYMENT-SIGNATURE = base64(envelope)",
      "read PAYMENT-RESPONSE (HTTP) or structuredContent.receipt (MCP)",
    ],
  };
}

// ------------------------------------------------------------------ storage

export async function indexPosts(env) {
  if (!env.DB) return { posts: [], count: 0, read_price_micro_algo: READ_PRICE, error: "storage not configured" };
  const { results } = await env.DB.prepare(
    "SELECT id, title, content_hash, bond_micro_algo, status, created_at FROM posts ORDER BY created_at DESC LIMIT 50",
  ).all();
  return { posts: results || [], count: (results || []).length, read_price_micro_algo: READ_PRICE };
}

async function fetchPost(env, id) {
  return env.DB.prepare(
    "SELECT id, author, title, body, content_hash, bond_micro_algo, status, created_at, window_seconds, txid FROM posts WHERE id = ?",
  ).bind(id).first();
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return bytesToHex(new Uint8Array(buf));
}

async function verifyHash(row) {
  const stored = String(row.content_hash || "");
  const expect = stored.startsWith("sha256:") ? stored.slice(7) : stored;
  if (!expect) return false;
  return (await sha256Hex(row.body || "")) === expect;
}

// ----------------------------------------------------------------- paid read

export async function readPost(env, cfg, id, paymentHeader) {
  if (!cfg.configured) return { status: 503, body: NOT_CONFIGURED };
  const row = await fetchPost(env, id);
  if (!row) return { status: 404, body: { error: `unknown post_id ${id}` } };

  const resource = `/api/posts/${id}`;
  if (!paymentHeader) {
    return { status: 402, body: await paymentRequired({ resource, amount: READ_PRICE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret }) };
  }
  try {
    const receipt = await verifyPayment({ headerB64: paymentHeader, resource, secret: cfg.secret, payTo: cfg.payTo, algod: cfg.algod, network: cfg.network });
    const verified = await verifyHash(row);
    return { status: 200, body: { post: { ...row, content_hash_verified: verified }, receipt } };
  } catch (err) {
    return {
      status: 402,
      body: await paymentRequired({ resource, amount: READ_PRICE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret, error: String(err.message || err) }),
    };
  }
}

// ---------------------------------------------------------------- paid write

export async function createPost(env, cfg, input, paymentHeader) {
  if (!cfg.configured) return { status: 503, body: NOT_CONFIGURED };
  const resource = "/api/posts";
  const contentHash = String(input.content_hash || "");
  const body = String(input.body || "");
  const windowSeconds = Number(input.window_seconds || 0);
  const bond = Number(input.bond_micro_algo || MIN_BOND);

  if (contentHash.length < 17) return { status: 400, body: { error: "content_hash must be at least 17 chars" } };
  if (windowSeconds < 60) return { status: 400, body: { error: "window_seconds must be >= 60" } };
  if (bond < MIN_BOND) return { status: 400, body: { error: `bond below min_bond ${MIN_BOND}` } };

  if (!paymentHeader) {
    return { status: 402, body: await paymentRequired({ resource, amount: CREATE_FEE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret }) };
  }

  let receipt;
  try {
    receipt = await verifyPayment({ headerB64: paymentHeader, resource, secret: cfg.secret, payTo: cfg.payTo, algod: cfg.algod, network: cfg.network });
  } catch (err) {
    return {
      status: 402,
      body: await paymentRequired({ resource, amount: CREATE_FEE, payTo: cfg.payTo, network: cfg.network, secret: cfg.secret, error: String(err.message || err) }),
    };
  }

  // duplicate content is an objective violation, not a judgment call
  const clash = await env.DB.prepare("SELECT id FROM posts WHERE content_hash = ?").bind(contentHash).first();
  if (clash) return { status: 409, body: { error: "duplicate content_hash", existing_post_id: clash.id } };

  const id = crypto.randomUUID().slice(0, 8);
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    "INSERT INTO posts (id, author, title, body, content_hash, bond_micro_algo, status, created_at, window_seconds, txid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(id, receipt.payer || "", String(input.title || ""), body, contentHash, bond, "UNBONDED", now, windowSeconds, receipt.transaction).run();

  return {
    status: 200,
    body: {
      post_id: id,
      status: "UNBONDED",
      fee_settled: { micro_algo: CREATE_FEE, transaction: receipt.transaction },
      atomic_group: [
        { index: 0, txn: "pay", from: "author", to: "app_escrow", amount_micro_algo: bond },
        { index: 1, txn: "appl", method: "post_bond(pay,uint64,byte[],uint64)", args: ["<group index 0>", id, contentHash, String(windowSeconds)] },
      ],
      next: `POST /api/posts/${id}/bond {"txid": "<post_bond group txid>"}`,
    },
  };
}

export async function confirmBond(env, cfg, id, txid) {
  const row = await fetchPost(env, id);
  if (!row) return { status: 404, body: { error: `unknown post_id ${id}` } };
  if (!txid) return { status: 400, body: { error: "txid required" } };
  if (!cfg.indexer) return { status: 501, body: { error: "bond confirmation requires INDEXER_URL" } };

  const r = await fetch(`${cfg.indexer.replace(/\/$/, "")}/v2/transactions/${txid}`);
  if (!r.ok) return { status: 400, body: { error: `transaction not found: ${txid}` } };
  const tx = await r.json();
  const appId = tx["application-transaction"]?.["application-id"];
  if (tx["tx-type"] !== "appl" || (cfg.appId && Number(appId) !== cfg.appId)) {
    return { status: 400, body: { error: "transaction is not an app call to this contract" } };
  }
  if (tx.sender !== row.author) return { status: 400, body: { error: "bond transaction was not sent by the post author" } };

  await env.DB.prepare("UPDATE posts SET status = 'OPEN', txid = ? WHERE id = ?").bind(txid, id).run();
  return { status: 200, body: { post_id: id, status: "OPEN", bond_txid: txid } };
}

// ------------------------------------------------------------------ challenge

export async function challengePlan(env, cfg, id) {
  const row = await fetchPost(env, id);
  if (!row) return { status: 404, body: { error: `unknown post_id ${id}` } };
  const required = row.bond_micro_algo;
  return {
    status: 200,
    body: {
      post_id: id,
      challenger_bond_micro_algo: required,
      atomic_group: [
        { index: 0, txn: "pay", from: "challenger", to: "app_escrow", amount_micro_algo: required },
        { index: 1, txn: "appl", method: "challenge(pay,uint64)", args: ["<group index 0>", id] },
      ],
    },
  };
}
