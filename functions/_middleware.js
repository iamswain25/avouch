// Cloudflare Pages middleware (runs for every route in _routes.json include)
// 1) www and *.pages.dev -> 301 apex (canonical host) — except /api/* and /mcp, so a
//    preview deployment can be exercised end to end on its own hostname
// 2) "/" -> 302 /ko/ or /en/ (cookie -> Accept-Language q-order -> English)
// Everything else falls through to the static asset.

const APEX = "avouchprotocol.com";

function pickLocale(request) {
  const cookie = request.headers.get("Cookie") || "";
  const m = cookie.match(/(?:^|;\s*)lang=(ko|en)\b/);
  if (m) return m[1];

  const header = request.headers.get("Accept-Language") || "";
  const tags = header
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.map((p) => p.trim()).find((p) => p.startsWith("q="));
      return { tag: tag.toLowerCase(), q: q ? parseFloat(q.slice(2)) : 1 };
    })
    .filter((t) => t.tag && !Number.isNaN(t.q))
    .sort((a, b) => b.q - a.q);

  for (const { tag } of tags) {
    const base = tag.split("-")[0];
    if (base === "ko") return "ko";
    if (base === "en") return "en";
  }
  return "en";
}

export async function onRequest(context) {
  const url = new URL(context.request.url);

  // API and MCP stay reachable on preview/deployment hostnames; only pages are canonicalised
  const isApi = url.pathname === "/mcp" || url.pathname.startsWith("/api/");

  if (!isApi && (url.hostname.startsWith("www.") || url.hostname.endsWith(".pages.dev"))) {
    return new Response(null, {
      status: 301,
      headers: {
        Location: `https://${APEX}${url.pathname}${url.search}`,
        "Cache-Control": "no-store",
        "Strict-Transport-Security": "max-age=15552000",
      },
    });
  }

  if (url.pathname === "/") {
    return new Response(null, {
      status: 302,
      headers: {
        Location: `/${pickLocale(context.request)}/`,
        Vary: "Accept-Language, Cookie",
        "Cache-Control": "no-store",
        "Strict-Transport-Security": "max-age=15552000",
      },
    });
  }

  return context.next();
}
