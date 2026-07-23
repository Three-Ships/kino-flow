/**
 * Kino Flow — shared-key gateway (Cloudflare Worker)
 *
 * The distributed desktop app must NEVER hold the shared provider keys. The
 * worker holds them (as encrypted Worker secrets) and injects them when
 * forwarding upstream. The app talks only to this worker.
 *
 *   Desktop app ──▶ this worker (holds real keys) ──▶ provider
 *
 * AUTH: no per-user accounts. A single OPTIONAL shared guard token:
 *   - If the GATEWAY_TOKEN secret is set, requests must send it as
 *     `Authorization: Bearer <GATEWAY_TOKEN>` (or x-api-key). This stops the
 *     worker URL, if leaked, from being an open faucet on your paid keys.
 *   - If GATEWAY_TOKEN is NOT set, the worker runs OPEN (any caller). Only do
 *     this if the URL stays private — a leak = uncapped spend on your account.
 *
 * Claude Code integration: set on the spawned `claude` process
 *   ANTHROPIC_BASE_URL = https://<worker-host>/anthropic
 *   ANTHROPIC_AUTH_TOKEN = <GATEWAY_TOKEN>   (only if you set the guard)
 * Claude Code appends /v1/messages → arrives as /anthropic/v1/messages.
 *
 * Secrets (set with `wrangler secret put <NAME>`):
 *   ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, HEYGEN_API_KEY,
 *   GEMINI_API_KEY, PIXABAY_API_KEY, PEXELS_API_KEY
 *   GATEWAY_TOKEN (optional shared guard token)
 */

// path-prefix -> upstream config. `inject` adds whatever auth the upstream
// expects, using the real key held server-side.
const UPSTREAMS = {
  anthropic: {
    base: "https://api.anthropic.com",
    secret: "ANTHROPIC_API_KEY",
    inject: (key, url, headers) => {
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
    inject: (key, url) => { url.searchParams.set("key", key); }, // Pixabay: ?key=
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

/** Pull the guard token from Authorization: Bearer or x-api-key. */
function extractToken(headers) {
  const auth = headers.get("authorization") || "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  const xkey = headers.get("x-api-key");
  if (xkey) return xkey.trim();
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight.
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
          "access-control-allow-headers": "authorization,x-api-key,content-type,anthropic-version,anthropic-beta",
        },
      });
    }

    // Health check (never echoes secrets).
    if (url.pathname === "/" || url.pathname === "/health") {
      return json(200, {
        ok: true,
        service: "kino-flow-gateway",
        upstreams: Object.keys(UPSTREAMS),
        guard: env.GATEWAY_TOKEN ? "token-required" : "OPEN (no guard token set)",
      });
    }

    // /<service>/<rest...>
    const parts = url.pathname.replace(/^\/+/, "").split("/");
    const service = parts.shift();
    const cfg = UPSTREAMS[service];
    if (!cfg) return json(404, { error: `unknown service '${service}'`, known: Object.keys(UPSTREAMS) });

    // Optional shared-token guard (checked before touching any real key).
    if (env.GATEWAY_TOKEN) {
      const token = extractToken(request.headers);
      if (token !== env.GATEWAY_TOKEN) return json(401, { error: "invalid or missing gateway token" });
    }

    const realKey = env[cfg.secret];
    if (!realKey) return json(500, { error: `gateway misconfigured: secret ${cfg.secret} not set` });

    // Build the upstream request: same path minus the /<service> prefix.
    const upstreamUrl = new URL(cfg.base);
    upstreamUrl.pathname = "/" + parts.join("/");
    upstreamUrl.search = url.search;

    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("x-api-key"); // drop the caller's guard token before forwarding
    cfg.inject(realKey, upstreamUrl, headers);

    // Forward (streaming pass-through: returning the upstream body streams it
    // straight back — this is why a Worker fits long Claude turns).
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
