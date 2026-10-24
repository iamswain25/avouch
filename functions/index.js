// Cloudflare Pages Function — locale routing for "/"
// Order: explicit cookie -> Accept-Language (first tag, q-order) -> English.
// Static pages live under /ko/ and /en/; this function only handles the root.

const LOCALES = new Set(["ko", "en"]);

function pickLocale(request) {
  const cookie = request.headers.get("Cookie") || "";
  const m = cookie.match(/(?:^|;\s*)lang=(ko|en)\b/);
  if (m && LOCALES.has(m[1])) return m[1];

  const header = request.headers.get("Accept-Language") || "";
  // "ko-KR,ko;q=0.9,en;q=0.8" -> ["ko-KR","ko","en"] in preference order
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

function redirect(lang) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: `/${lang}/`,
      Vary: "Accept-Language, Cookie",
      "Cache-Control": "no-store",
    },
  });
}

export const onRequestGet = ({ request }) => redirect(pickLocale(request));
export const onRequestHead = ({ request }) => redirect(pickLocale(request));
