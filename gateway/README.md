# Kino Flow — shared-key gateway

A single Cloudflare Worker that fronts every shared provider API (Anthropic,
ElevenLabs, HeyGen, Gemini, Pixabay, Pexels) for the distributed desktop app.

**Why:** the app ships to users' machines. If it held the real keys, anyone
could extract them. Instead this worker holds the keys as encrypted Cloudflare
secrets and injects them when forwarding upstream — the keys never touch a
user's machine.

```
Desktop app ──▶ gateway (holds real keys) ──▶ provider
```

**Auth:** no per-user accounts. One OPTIONAL shared guard token (`GATEWAY_TOKEN`):
set it and callers must send `Authorization: Bearer <token>`; leave it unset and
the worker is open to anyone with the URL (only safe if the URL stays private —
a leak means uncapped spend on your account).

## Deploy (one time)

```bash
cd gateway
npm install
npx wrangler login          # opens a browser to authorize your Cloudflare account

# Load the shared keys as encrypted secrets (prompts you to paste each value)
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY
npx wrangler secret put HEYGEN_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put PIXABAY_API_KEY
npx wrangler secret put PEXELS_API_KEY
npx wrangler secret put GATEWAY_TOKEN     # optional guard (recommended)

npx wrangler deploy
```

Deploy prints your worker URL, e.g. `https://kino-flow-gateway.<subdomain>.workers.dev`.

## Point the app at the gateway

For Claude, set on the spawned `claude` process (see `studio/server.py`):

```
ANTHROPIC_BASE_URL   = https://<worker-host>/anthropic
ANTHROPIC_AUTH_TOKEN = <GATEWAY_TOKEN>        # only if you set the guard
```

Media helpers use the other prefixes: `…/elevenlabs/v1/…`, `…/heygen/v2/…`,
`…/gemini/v1beta/…`, `…/pixabay/api/…`, `…/pexels/v1/…`.

## Test

```bash
curl -s https://<worker-host>/health
curl -s https://<worker-host>/anthropic/v1/messages \
  -H "authorization: Bearer <GATEWAY_TOKEN>" \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

Local testing: `cp .dev.vars.example .dev.vars`, fill it in, `npm run dev`,
then hit `localhost:8787`.
