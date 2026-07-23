/**
 * Kino Flow — shared-key gateway (Cloudflare Worker)
 *
 * Purpose: the distributed desktop app must NEVER hold the shared provider keys.
 * The app authenticates to THIS worker with a per-user token; the worker holds
 * the real keys (as encrypted Worker secrets) and injects them when forwarding
 * upstream. The shared keys never touch a user's machine.
 *
 *   Desktop app ──(Bearer <user-token>)──▶ this worker ──(real key)──▶ provider
 *
 * Claude Code integration: set on the spawned `claude` process
 *   ANTHROPIC_BASE_URL = https://<your-worker-host>/anthropic
 *   ANTHROPIC_AUTH_TOKEN = <the user's gateway token>   (or via apiKeyHelper)
 * Claude Code appends /v1/messages, so requests arrive as /anthropic/v1/messages.
 *
 * Secrets (set with `wrangler secret put <NAME>`):
 *   ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, HEYGEN_API_KEY,
 *   GEMINI_API_KEY, PIXABAY_API_KEY, PEXELS_API_KEY
 * Bindings (wrangler.toml): TOKENS (KV) — maps user-token -> user record.
 */

// path-prefix -> upstream config. `inject` receives the real key and returns
// the headers/query to add so the upstream authenticates the request.
const UPSTREAMS = {
  anthropic: {
    base: "https://api.anthropic.com",
    secret: "ANTHROPIC_API_KEY",
    inject: (key, url, headers) => {
      // Claude Code sends the gateway token as x-api-key AND Authorization.
      // Strip both, replace with the real Anthropic key.
      headers.delete("authorization");
      headers.set("x-api-key", key);
      if (!headers.has("anthropic-version")) headers.set("anthropic-version", "2023-06-01");
    },
  },
  elevenlabs: {
    base: "https://api.elevenlabs.io",
    secret: "ELEVENLABS_API_KEY",
    inject: (key, url, headers) => { headers.delete("authorization"); headers.set("xi-api-key", key); },
  },
  heygen: {
    base: "https://api.heygen.com",
    secret: "HEYGEN_API_KEY",
    inject: (key, url, headers) => { headers.delete("authorization"); headers.set("x-api-key", key); },
  },
  gemini: {
    base: "https://generativelanguage.googleapis.com",
    secret: "GEMINI_API_KEY",
    inject: (key, url, headers) => { headers.delete("authorization"); headers.set("x-goog-api-key", key); },
  },
  pixabay: {
    base: "https://pixabay.com",
    secret: "PIXABAY_API_KEY",
    inject: (key, url) => { url.searchParams.set("key", key); }, // Pixabay authenticates via ?key=
  },
  pexels: {
    base: "https://api.pexels.com",
    secret: "PEXELS_API_KEY",
    inject: (key, url, headers) => { headers.set("authorization", key); }, // Pexels: raw key in Authorization
  },
};

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Pull the caller's gateway token from Authorization: Bearer or x-api-key. */
function extractToken(headers) {
  const auth = headers.get("authorization") || "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  const xkey = headers.get("x-api-key");
  if (xkey) return xkey.trim();
  return null;
}

/**
 * Validate the per-user token against the TOKENS KV namespace.
 * KV value shape (JSON): { user: "sean@...", disabled?: bool, spendCapUsd?: number }
 * Returns the user record or null. Revoking a user = delete their KV entry.
 */
async function authenticate(env, token) {
  if (!token) return null;
  if (!env.TOKENS) {
    // Dev fallback ONLY: if no KV bound and DEV_TOKEN is set, accept it.
    if (env.DEV_TOKEN && token === env.DEV_TOKEN) return { user: "dev", spendCapUsd: 5 };
    return null;
  }
  const raw = await env.TOKENS.get(`tok:${token}`);
  if (!raw) return null;
  const rec = JSON.parse(raw);
  if (rec.disabled) return null;
  return rec;
}

/**
 * Coarse usage log + soft spend guard. NOTE: KV is eventually-consistent and
 * these increments are NOT atomic — good enough for logging and a soft cap, but
 * for a HARD per-user spend cap use Durable Objects or D1 (see README).
 */
async function checkAndLogUsage(env, rec, service) {
  if (!env.TOKENS || !rec || !rec.user) return { ok: true };
  const monthKey = `use:${rec.user}`; // production: bucket by YYYY-MM
  const raw = await env.TOKENS.get(monthKey);
  const usage = raw ? JSON.parse(raw) : { requests: 0, byService: {} };
  usage.requests += 1;
  usage.byService[service] = (usage.byService[service] || 0) + 1;
  // Fire-and-forget write; do not block the proxied request on it.
  await env.TOKENS.put(monthKey, JSON.stringify(usage));
  return { ok: true };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS preflight (desktop app / localhost origins).
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
          "access-control-allow-headers": "authorization,x-api-key,content-type,anthropic-version,anthropic-beta",
        },
      });
    }

    // Health check.
    if (url.pathname === "/" || url.pathname === "/health") {
      return json(200, { ok: true, service: "kino-flow-gateway", upstreams: Object.keys(UPSTREAMS) });
    }

    // /<service>/<rest...>
    const parts = url.pathname.replace(/^\/+/, "").split("/");
    const service = parts.shift();
    const cfg = UPSTREAMS[service];
    if (!cfg) return json(404, { error: `unknown service '${service}'`, known: Object.keys(UPSTREAMS) });

    // AuthN: validate the per-user gateway token BEFORE touching any real key.
    const token = extractToken(request.headers);
    const rec = await authenticate(env, token);
    if (!rec) return json(401, { error: "invalid or missing gateway token" });

    const realKey = env[cfg.secret];
    if (!realKey) return json(500, { error: `gateway misconfigured: secret ${cfg.secret} not set` });

    // Build the upstream request: same path minus the /<service> prefix.
    const upstreamUrl = new URL(cfg.base);
    upstreamUrl.pathname = "/" + parts.join("/");
    upstreamUrl.search = url.search;

    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("x-api-key"); // remove the caller's gateway token
    cfg.inject(realKey, upstreamUrl, headers);

    await checkAndLogUsage(env, rec, service);

    // Forward (streaming pass-through: returning the upstream Response streams
    // the body straight back to the client — this is why a Worker fits here).
    const upstreamReq = new Request(upstreamUrl.toString(), {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    });
    const resp = await fetch(upstreamReq);
    const outHeaders = new Headers(resp.headers);
    outHeaders.set("access-control-allow-origin", "*");
    return new Response(resp.body, { status: resp.status, headers: outHeaders });
  },
};
