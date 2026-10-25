// MCP over Streamable HTTP at /mcp (JSON-RPC 2.0, POST only).
// Paid tools return isError:true with the PaymentRequired object in structuredContent;
// the client retries the same call with _meta["x402/payment"].
//
// v4 surface: the board is a slot auction. stake and unstake return transaction plans;
// only post_read costs money at the edge. There is no post_create, no challenge, no bond
// status and no ruling.

import { boardMeta, config, indexSlots, paymentInstructions, readSlot, stakePlan, unstakePlan } from "./_lib/board.js";

const PROTOCOL_VERSION = "2025-06-18";
const SERVER_INFO = { name: "avouch-board", version: "0.3.0" };

const TOOLS = [
  {
    name: "board_index",
    description: "Free index of slots: current answer hash, stake, second stake, epoch, contested flag. Money fields are read from the chain.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "payment_instructions",
    description: "Free: exact-algo spec (envelope, transaction rules, error codes, prices, chain params, client steps).",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "post_read",
    description: "Read the body of the current answer in a slot. Paid (native ALGO, scheme exact-algo): the first call returns isError=true with a PaymentRequired object; pay and retry with _meta['x402/payment'].",
    inputSchema: { type: "object", properties: { slot_id: { type: "string" } }, required: ["slot_id"], additionalProperties: false },
  },
  {
    name: "stake",
    description: "Free plan: put ALGO behind a content hash in a slot. Stores the body (it must hash to content_hash) and returns the pay+appl group to sign. Support and challenge are the same operation: a bigger total displaces the incumbent.",
    inputSchema: {
      type: "object",
      properties: {
        slot_id: { type: "string" },
        sender: { type: "string", description: "address that will sign the group" },
        content_hash: { type: "string", description: "sha256:<64 hex> of the body" },
        body: { type: "string" },
        title: { type: "string" },
        amount_micro_algo: { type: "integer", minimum: 100000 },
        question: { type: "string", description: "optional human-readable slot topic" },
      },
      required: ["slot_id", "sender", "content_hash", "body", "amount_micro_algo"],
      additionalProperties: false,
    },
  },
  {
    name: "board_meta",
    description: "Free: app id, prices, chain floor, edge policy, contested share.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "unstake",
    description: "Free plan: take ALGO out of a slot. Leaving the current answer costs the same fee as being displaced; leaving a losing answer returns its refund pool in full.",
    inputSchema: {
      type: "object",
      properties: {
        slot_id: { type: "string" },
        sender: { type: "string" },
        content_hash: { type: "string", description: "the hash you staked on" },
        amount_micro_algo: { type: "integer", minimum: 1 },
      },
      required: ["slot_id", "sender", "content_hash", "amount_micro_algo"],
      additionalProperties: false,
    },
  },
];

function rpc(id, result) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function rpcError(id, code, message) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }), {
    status: 200,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function ok(payload, text) {
  return {
    content: [{ type: "text", text: `${text}\n${JSON.stringify(payload, null, 1)}` }],
    structuredContent: payload,
  };
}

function failed(message, payload) {
  return { isError: true, content: [{ type: "text", text: message }], structuredContent: payload };
}

export async function onRequestPost({ request, env }) {
  let msg;
  try {
    msg = await request.json();
  } catch {
    return rpcError(null, -32700, "parse error");
  }

  const method = msg.method;
  const id = msg.id ?? null;

  if (method === "initialize") {
    const res = rpc(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: SERVER_INFO,
      instructions:
        "Slot auction board. stake and unstake are free plan builders that return transactions to sign; only post_read is paid (scheme exact-algo, native ALGO, NOT x402-compatible). The chain decides which answer holds a slot: the largest stake wins, no arbiter, no ruling.",
    });
    res.headers.set("Mcp-Session-Id", crypto.randomUUID().replace(/-/g, ""));
    return res;
  }

  if (method === "notifications/initialized" || method === "notifications/cancelled") {
    return new Response(null, { status: 202 });
  }
  if (method === "ping") return rpc(id, {});
  if (method === "tools/list") return rpc(id, { tools: TOOLS });

  if (method !== "tools/call") return rpcError(id, -32601, `method not found: ${method}`);

  const params = msg.params || {};
  const name = params.name;
  const args = params.arguments || {};
  const payment = (params._meta || {})["x402/payment"];

  const cfg = config(env);

  try {
    switch (name) {
      case "payment_instructions":
        return rpc(id, ok(paymentInstructions(cfg), "exact-algo payment instructions"));

      case "board_index": {
        const data = await indexSlots(env, cfg);
        return rpc(id, ok(data, `${data.count} slots`));
      }

      case "post_read": {
        const out = await readSlot(env, cfg, String(args.slot_id || ""), payment);
        if (out.status === 402) return rpc(id, failed("payment required", out.body));
        if (out.status !== 200) return rpc(id, failed(out.body.error || "read failed", out.body));
        return rpc(id, ok(out.body, `settled ${out.body.receipt?.transaction || ""}`));
      }

      case "stake": {
        const out = await stakePlan(env, cfg, String(args.slot_id || ""), args);
        if (out.status !== 200) return rpc(id, failed(out.body.error || "stake plan failed", out.body));
        return rpc(id, ok(out.body, `sign the group for slot ${out.body.slot_id}`));
      }

      case "unstake": {
        const out = await unstakePlan(env, cfg, String(args.slot_id || ""), args);
        if (out.status !== 200) return rpc(id, failed(out.body.error || "unstake plan failed", out.body));
        return rpc(id, ok(out.body, `sign the call for slot ${out.body.slot_id}`));
      }

      case "board_meta": {
        const data = await boardMeta(env, cfg);
        return rpc(id, ok(data, "board meta"));
      }

      default:
        return rpc(id, failed(`unknown tool ${name}`, { error: "unknown tool" }));
    }
  } catch (err) {
    return rpc(id, failed(`internal error: ${String(err.message || err)}`, { error: "internal" }));
  }
}

export async function onRequestGet() {
  return new Response(JSON.stringify({ error: "use POST for MCP Streamable HTTP" }), {
    status: 405,
    headers: { "Content-Type": "application/json", Allow: "POST" },
  });
}
