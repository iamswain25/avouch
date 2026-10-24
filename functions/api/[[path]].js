// REST surface for the paid board. Same payment gate as the MCP tools.
//
//   GET  /api/posts                  free index
//   GET  /api/posts/:id              paid body (402 exact-algo)
//   POST /api/posts                  paid fee, returns the bond group to sign
//   POST /api/posts/:id/bond         mark a post bonded after the app call confirms
//   POST /api/posts/:id/challenge    challenger bond plan

import { challengePlan, config, confirmBond, createPost, indexPosts, readPost } from "../_lib/board.js";

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

  try {
    // /api/posts
    if (seg.length === 1 && seg[0] === "posts") {
      if (method === "GET") return json(await indexPosts(env));
      if (method === "POST") {
        const input = await request.json().catch(() => ({}));
        const out = await createPost(env, cfg, input, payment);
        return json(out.body, out.status);
      }
      if (method === "OPTIONS") return new Response(null, { status: 204, headers: { Allow: "GET, POST" } });
      return json({ error: "method not allowed" }, 405);
    }

    // /api/posts/:id/bond and /api/posts/:id/challenge
    if (seg.length === 3 && seg[0] === "posts" && method === "POST") {
      const [, id, action] = seg;
      if (action === "bond") {
        const input = await request.json().catch(() => ({}));
        const out = await confirmBond(env, cfg, id, input.txid);
        return json(out.body, out.status);
      }
      if (action === "challenge") {
        const out = await challengePlan(env, cfg, id);
        return json(out.body, out.status);
      }
    }

    // /api/posts/:id
    if (seg.length === 2 && seg[0] === "posts" && method === "GET") {
      const out = await readPost(env, cfg, seg[1], payment);
      return json(out.body, out.status);
    }

    return json({ error: "not found", path: seg.join("/") }, 404);
  } catch (err) {
    return json({ error: String(err.message || err), storage: Boolean(env.DB), payments: cfg.configured }, 500);
  }
}
