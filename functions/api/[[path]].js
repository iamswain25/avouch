// REST surface for the v4 slot auction. Bodies and plans live here; money lives on chain.
//
//   GET  /api/posts                 free index (chain state per slot)
//   GET  /api/posts/:slot           paid body of the current answer (402 exact-algo)
//   POST /api/posts/:slot/stake     free plan: pay + appl group to put ALGO behind a hash
//   POST /api/posts/:slot/unstake   free plan: appl call to take ALGO out
//   GET  /api/meta                  free: app id, prices, floor, policy, contested share
//   POST /api/credit/topup          free plan: one payment that funds N reads
//   POST /api/credit/redeem/:id     paid: turn that payment into a credit token
//   GET  /api/credit/:id            free: balance, reads left, expiry (needs the token)

import { boardMeta, config, creditInfo, creditRedeem, creditTopup, indexSlots, readSlot, stakePlan, unstakePlan } from "../_lib/board.js";

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 1), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export async function onRequest(context) {
  const { request, env } = context;
  try {
    return await route(request, env, context);
  } catch (err) {
    return json({ error: String(err && err.message ? err.message : err) }, 500);
  }
}

async function route(request, env, context) {
  const cfg = config(env);
  const url = new URL(request.url);
  const fromParams = context && context.params && context.params.path;
  const seg = Array.isArray(fromParams) && fromParams.length
    ? fromParams
    : url.pathname.replace(/^\/api\/?/, "").split("/").filter(Boolean);
  const method = request.method.toUpperCase();
  const payment = request.headers.get("PAYMENT-SIGNATURE") || request.headers.get("X-PAYMENT");
  const credit = request.headers.get("X-CREDIT") || request.headers.get("X-CREDIT-TOKEN");

  try {
    if (seg.length === 1 && seg[0] === "meta" && method === "GET") {
      return json(await boardMeta(env, cfg));
    }

    if (seg.length === 1 && seg[0] === "posts") {
      if (method === "GET") return json(await indexSlots(env, cfg));
      if (method === "OPTIONS") return new Response(null, { status: 204, headers: { Allow: "GET" } });
      return json({ error: "method not allowed; the board is a slot auction, use /api/posts/:slot/stake" }, 405);
    }

    if (seg.length === 3 && seg[0] === "posts" && method === "POST") {
      const [, slot, action] = seg;
      const input = await request.json().catch(() => ({}));
      if (action === "stake") {
        const out = await stakePlan(env, cfg, slot, input);
        return json(out.body, out.status);
      }
      if (action === "unstake") {
        const out = await unstakePlan(env, cfg, slot, input);
        return json(out.body, out.status);
      }
    }

    if (seg.length === 2 && seg[0] === "posts" && method === "GET") {
      const out = await readSlot(env, cfg, seg[1], payment, credit);
      return json(out.body, out.status);
    }

    // prepaid reads: one payment, many reads
    if (seg.length === 2 && seg[0] === "credit" && seg[1] === "topup" && method === "POST") {
      const input = await request.json().catch(() => ({}));
      const out = await creditTopup(env, cfg, input);
      return json(out.body, out.status);
    }
    if (seg.length === 3 && seg[0] === "credit" && seg[1] === "redeem" && method === "POST") {
      const out = await creditRedeem(env, cfg, seg[2], payment);
      return json(out.body, out.status);
    }
    if (seg.length === 2 && seg[0] === "credit" && method === "GET") {
      const out = await creditInfo(env, cfg, seg[1], credit || url.searchParams.get("token"));
      return json(out.body, out.status);
    }

    return json({ error: "not found", path: seg.join("/") }, 404);
  } catch (err) {
    return json({ error: String(err.message || err), storage: Boolean(env.DB), payments: cfg.configured, chain: Boolean(cfg.appId) }, 500);
  }
}
