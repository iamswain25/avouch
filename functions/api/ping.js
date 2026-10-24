export function onRequestGet({ env }) {
  return new Response(JSON.stringify({
    ok: true,
    storage: Boolean(env.DB),
    payments: Boolean(env.X402_SECRET && env.PAY_TO && env.ALGOD_URL),
  }), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}
