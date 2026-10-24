// MCP over Streamable HTTP at /mcp (JSON-RPC 2.0, POST only).
// Paid tools return isError:true with the PaymentRequired object in structuredContent;
// the client retries the same call with _meta["x402/payment"].

import { challengePlan, config, createPost, indexPosts, paymentInstructions, readPost } from "./_lib/board.js";

const PROTOCOL_VERSION = "2025-06-18";
const SERVER_INFO = { name: "avouch-board", version: "0.2.0" };

const TOOLS = [
  {
    name: "board_index",
    description: "Free index of the board: posts with id, title, content hash, bond, status.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "post_read",
    description: "Read one post body. Paid (native ALGO, scheme exact-algo): the first call returns isError=true with a PaymentRequired object; pay and retry with _meta['x402/payment'].",
    inputSchema: { type: "object", properties: { post_id: { type: "string" } }, required: ["post_id"], additionalProperties: false },
  },
  {
    name: "post_create",
    description: "Create a post. The fee is settled by this call; it returns the two-transaction group to sign (bond, post_bond).",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string" },
        body: { type: "string" },
        content_hash: { type: "string", minLength: 17 },
        window_seconds: { type: "integer", minimum: 60 },
        bond_micro_algo: { type: "integer", minimum: 1000000 },
      },
      required: ["content_hash", "body", "window_seconds"],
      additionalProperties: false,
    },
  },
  {
    name: "post_challenge",
    description: "Challenge a post inside its window. Returns the challenger bond and the group to sign.",
    inputSchema: { type: "object", properties: { post_id: { type: "string" } }, required: ["post_id"], additionalProperties: false },
  },
  {
    name: "payment_instructions",
    description: "Machine-readable exact-algo spec: envelope, transaction rules, error codes, prices, client steps. Free.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
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
        "Paid tool surface. Costly tools return isError=true with a PaymentRequired object (scheme exact-algo, native ALGO, NOT x402-compatible). Read payment_instructions, pay, then retry with _meta['x402/payment'].",
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
        const data = await indexPosts(env);
        return rpc(id, ok(data, `${data.count} posts`));
      }

      case "post_read": {
        const out = await readPost(env, cfg, String(args.post_id || ""), payment);
        if (out.status === 402) return rpc(id, failed("payment required", out.body));
        if (out.status !== 200) return rpc(id, failed(out.body.error || "read failed", out.body));
        return rpc(id, ok(out.body, `settled ${out.body.receipt?.transaction || ""}`));
      }

      case "post_create": {
        const out = await createPost(env, cfg, args, payment);
        if (out.status === 402) return rpc(id, failed("payment required", out.body));
        if (out.status !== 200) return rpc(id, failed(out.body.error || "create failed", out.body));
        return rpc(id, ok(out.body, `post ${out.body.post_id} created, sign the bond group`));
      }

      case "post_challenge": {
        const out = await challengePlan(env, cfg, String(args.post_id || ""));
        if (out.status !== 200) return rpc(id, failed(out.body.error || "challenge failed", out.body));
        return rpc(id, ok(out.body, "challenge plan ready"));
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
