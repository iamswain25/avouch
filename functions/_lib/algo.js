// Algorand payment verification for the edge: msgpack decode, stateless challenges,
// algod submit/confirm. No signature verification here — the chain does that.

const MIN_FEE = 1000;
const MAX_FEE = 2 * MIN_FEE;
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function bytesToB64(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// base32 (RFC 4648, no padding) — Algorand addresses

export function decodeAddress(addr) {
  const clean = addr.trim().toUpperCase().replace(/=+$/, "");
  let bits = 0;
  let value = 0;
  const out = [];
  for (const ch of clean) {
    const idx = B32.indexOf(ch);
    if (idx === -1) throw new Error("invalid address character");
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  const bytes = new Uint8Array(out);
  if (bytes.length !== 36) throw new Error("invalid address length");
  return bytes.slice(0, 32); // drop the 4-byte checksum
}

// Minimal msgpack decoder: maps, arrays, strings, bin, ints, nil, bool, floats.
export function msgpackDecode(bytes) {
  let pos = 0;
  const need = (n) => {
    if (pos + n > bytes.length) throw new Error("msgpack: truncated");
    const s = bytes.subarray(pos, pos + n);
    pos += n;
    return s;
  };
  const u8 = () => need(1)[0];
  const u16 = () => {
    const b = need(2);
    return (b[0] << 8) | b[1];
  };
  const u32 = () => {
    const b = need(4);
    return ((b[0] << 24) >>> 0) + (b[1] << 16) + (b[2] << 8) + b[3];
  };
  const u64 = () => {
    const b = need(8);
    let v = 0n;
    for (const x of b) v = (v << 8n) | BigInt(x);
    return v;
  };
  const str = (n) => new TextDecoder().decode(need(n));
  const obj = (n) => {
    const o = {};
    for (let i = 0; i < n; i++) o[decode()] = decode();
    return o;
  };
  const arr = (n) => {
    const a = [];
    for (let i = 0; i < n; i++) a.push(decode());
    return a;
  };
  function decode() {
    const b = u8();
    if (b <= 0x7f) return b;
    if (b >= 0xe0) return b - 0x100;
    if ((b & 0xf0) === 0x80) return obj(b & 0x0f);
    if ((b & 0xf0) === 0x90) return arr(b & 0x0f);
    if ((b & 0xe0) === 0xa0) return str(b & 0x1f);
    switch (b) {
      case 0xc0: return null;
      case 0xc2: return false;
      case 0xc3: return true;
      case 0xc4: return need(u8()).slice();
      case 0xc5: return need(u16()).slice();
      case 0xc6: return need(u32()).slice();
      case 0xca: need(4); return NaN;
      case 0xcb: need(8); return NaN;
      case 0xcc: return u8();
      case 0xcd: return u16();
      case 0xce: return u32();
      case 0xcf: { const v = u64(); return v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : v; }
      case 0xd0: { const v = u8(); return v > 127 ? v - 256 : v; }
      case 0xd1: { const v = u16(); return v > 32767 ? v - 65536 : v; }
      case 0xd2: { const v = u32(); return v > 2147483647 ? v - 4294967296 : v; }
      case 0xd3: { const v = u64(); const s = v & (1n << 63n) ? v - (1n << 64n) : v; return Number(s); }
      case 0xd9: return str(u8());
      case 0xda: return str(u16());
      case 0xdb: return str(u32());
      case 0xdc: return arr(u16());
      case 0xdd: return arr(u32());
      case 0xde: return obj(u16());
      case 0xdf: return obj(u32());
      default: throw new Error(`msgpack: unsupported type 0x${b.toString(16)}`);
    }
  }
  return decode();
}

// ---------------------------------------------------------------- challenges

const enc = new TextEncoder();

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return bytesToB64(new Uint8Array(sig)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64url(s) {
  return bytesToB64(enc.encode(s)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function makeChallenge(secret, { resource, amount, ttlSeconds = 120 }) {
  const payload = { r: resource, a: amount, e: Math.floor(Date.now() / 1000) + ttlSeconds, n: bytesToHex(crypto.getRandomValues(new Uint8Array(8))) };
  const body = b64url(JSON.stringify(payload));
  return { paymentId: `${body}.${await hmac(secret, body)}`, payload };
}

export async function readChallenge(secret, paymentId, resource) {
  const [body, sig] = String(paymentId).split(".");
  if (!body || !sig) throw new Error("malformed paymentId");
  const expected = await hmac(secret, body);
  if (expected.length !== sig.length) throw new Error("bad paymentId signature");
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  if (diff !== 0) throw new Error("bad paymentId signature");
  const payload = JSON.parse(new TextDecoder().decode(b64ToBytes(body.replace(/-/g, "+").replace(/_/g, "/"))));
  if (payload.e < Math.floor(Date.now() / 1000)) throw new Error("challenge expired");
  if (payload.r !== resource) throw new Error("paymentId was issued for a different resource");
  return payload;
}

// -------------------------------------------------------------------- algod

export class Algod {
  constructor(baseUrl, token) {
    this.url = String(baseUrl || "").replace(/\/$/, "");
    this.token = token || "";
  }

  headers(extra = {}) {
    const h = { ...extra };
    if (this.token) h["X-Algo-API-Token"] = this.token;
    return h;
  }

  async params() {
    const r = await fetch(`${this.url}/v2/transactions/params`, { headers: this.headers() });
    if (!r.ok) throw new Error(`algod params ${r.status}`);
    const p = await r.json();
    return { lastRound: p["last-round"], genesisHash: p["genesis-hash"], minFee: p["min-fee"] };
  }

  async submit(bytes) {
    const r = await fetch(`${this.url}/v2/transactions`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/x-binary" }),
      body: bytes,
    });
    const text = await r.text();
    if (!r.ok) {
      // a payment that already landed is not a failure: a client retrying after a slow
      // confirmation must still be able to redeem it, so recover the txid and wait instead
      if (/already in ledger|already in pool|already committed/i.test(text)) {
        const txid = (text.match(/[A-Z2-7]{52}/) || [])[0];
        if (txid) return txid;
      }
      throw new Error(`settlement failed: ${text.slice(0, 200)}`);
    }
    // algod returns either a bare JSON string or {"txId": "..."} depending on version
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && parsed.txId) return parsed.txId;
    } catch {
      /* not json, fall through */
    }
    return text.trim().replace(/^"|"$/g, "");
  }

  async box(appId, nameBytes) {
    const name = encodeURIComponent(bytesToB64(nameBytes));
    const r = await fetch(`${this.url}/v2/applications/${appId}/box?name=b64:${name}`, { headers: this.headers() });
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`algod box ${r.status}`);
    const j = await r.json();
    return b64ToBytes(j.value);
  }

  async waitFor(txid, rounds = 40) {
    // TestNet blocks are ~2.8s and Nodely adds latency, so a 4s budget timeouts while the
    // payment is still landing. Confirmation is what the credit needs, so wait up to ~20s.
    for (let i = 0; i < rounds; i++) {
      const r = await fetch(`${this.url}/v2/transactions/pending/${txid}`, { headers: this.headers() });
      if (r.ok) {
        const info = await r.json();
        if (info["confirmed-round"]) return info;
        if (info["pool-error"]) throw new Error(`settlement failed: ${info["pool-error"]}`);
      }
      await new Promise((res) => setTimeout(res, 500));
    }
    throw new Error("settlement failed: not confirmed in time");
  }
}

// --------------------------------------------------------- payment envelope

export function decodeEnvelope(headerB64) {
  let env;
  try {
    env = JSON.parse(new TextDecoder().decode(b64ToBytes(headerB64)));
  } catch {
    throw new Error("payment header is not base64 json");
  }
  if (env.x402Version !== 2) throw new Error("x402Version must be 2");
  if (env.scheme !== "exact") throw new Error("scheme must be exact");
  const txnB64 = env?.payload?.txn;
  if (!txnB64) throw new Error("payload.txn missing");
  const signed = msgpackDecode(b64ToBytes(txnB64));
  const txn = signed?.txn;
  if (!txn || typeof txn !== "object") throw new Error("payload.txn is not a signed transaction");
  return { env, signed, txn, txnBytes: b64ToBytes(txnB64) };
}

export async function verifyPayment({ headerB64, resource, secret, payTo, algod, network }) {
  const { env, txn, txnBytes } = decodeEnvelope(headerB64);

  if (env.network !== network) throw new Error(`network mismatch: ${env.network}`);
  if (txn.type !== "pay") throw new Error(`expected a pay transaction, got ${txn.type}`);
  if (txn.grp) throw new Error("grouped transactions are not accepted");
  if (txn.rekey) throw new Error("rekey_to is not allowed");
  if (txn.close) throw new Error("close_remainder_to is not allowed");

  const note = txn.note ? new TextDecoder().decode(txn.note) : "";
  const ch = await readChallenge(secret, note, resource);

  const params = await algod.params();
  if (!txn.rcv || bytesToHex(decodeAddress(payTo)) !== bytesToHex(txn.rcv)) throw new Error("receiver mismatch");
  if (Number(txn.amt) < Number(ch.a)) throw new Error(`underpaid: ${txn.amt} < ${ch.a}`);
  if (Number(txn.fee || 0) > MAX_FEE) throw new Error("fee above the accepted cap");
  if (Number(txn.lv) < Number(params.lastRound)) throw new Error("transaction expired");
  if (params.genesisHash) {
    const gh = b64ToBytes(params.genesisHash);
    if (bytesToHex(gh) !== bytesToHex(txn.gh || new Uint8Array())) throw new Error("wrong network: genesis hash mismatch");
  }

  const txid = await algod.submit(txnBytes);
  const info = await algod.waitFor(txid);
  return {
    success: true,
    transaction: txid,
    network,
    confirmedRound: info["confirmed-round"],
    // some nodes omit sender in the pending info; the refund job resolves the payer from the
    // deposit transaction instead of trusting this field
    payer: info.sender || null,
    amount: Number(txn.amt),
  };
}

export function paymentRequired({ resource, amount, payTo, network, error, secret }) {
  return makeChallenge(secret, { resource, amount }).then(({ paymentId }) => ({
    x402Version: 2,
    error: error || "payment required",
    accepts: [{
      scheme: "exact",
      network,
      asset: "ALGO",
      maxAmountRequired: String(amount),
      resource,
      description: `paid access to ${resource}`,
      payTo,
      maxTimeoutSeconds: 120,
      extra: { paymentId, minFeeMicroAlgo: MIN_FEE },
    }],
    how_to_pay: { tool: "payment_instructions", scheme: "exact-algo" },
  }));
}
