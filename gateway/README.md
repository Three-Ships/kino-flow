# Kino Flow — shared-key gateway

A single Cloudflare Worker that fronts every shared provider API (Anthropic,
ElevenLabs, HeyGen, Gemini, Pixabay, Pexels) for the distributed desktop app.

**Why:** the app ships to users' machines. If it held the real keys, anyone
could extract them. Instead the app holds a **per-user token**; this worker
holds the real keys as encrypted Cloudflare secrets and injects them when
forwarding upstream. Revoke a user by deleting their token — no key rotation.

```
Desktop app ──Bearer <user-token>──▶ gateway (holds real keys) ──▶ provider
```

## One-time setup

```bash
cd gateway
npm install
npx wrangler login

# 1) Create the per-user token store, paste the id into wrangler.toml
npx wrangler kv namespace create TOKENS

# 2) Load the shared keys as encrypted secrets (repeat for each)
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY
npx wrangler secret put HEYGEN_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put PIXABAY_API_KEY
npx wrangler secret put PEXELS_API_KEY

# 3) Deploy
npx wrangler deploy
```

## Issue a per-user token

```bash
# token -> user record; delete the key to revoke access instantly
npx wrangler kv key put --binding=TOKENS "tok:USER_TOKEN_HERE" \
  '{"user":"sean@homesolutions.com","spendCapUsd":25}'
```

In production these are minted automatically after Supabase Auth login rather
than by hand.

## Point Claude Code at the gateway (in the desktop app)

Set on the spawned `claude` process (see `studio/server.py`):

```
ANTHROPIC_BASE_URL = https://<your-worker-host>/anthropic
ANTHROPIC_AUTH_TOKEN = <the user's gateway token>
```

Media helpers call the other prefixes, e.g. `…/elevenlabs/v1/text-to-speech/…`,
`…/heygen/v2/…`, `…/gemini/v1beta/…`, `…/pixabay/api/…`, `…/pexels/v1/…`.

## Local testing

```bash
cp .dev.vars.example .dev.vars   # fill in keys + a DEV_TOKEN
npm run dev
curl -s localhost:8787/health
curl -s localhost:8787/anthropic/v1/messages -H "authorization: Bearer $DEV_TOKEN" \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

## Production hardening (not in the starter)

- **Hard spend caps:** KV increments here are non-atomic (fine for logging + a
  soft cap). For enforced per-user limits use **Durable Objects** or **D1** to
  count tokens/cost atomically, and read real usage from provider response
  headers.
- **Global kill-switch:** a single KV flag the worker checks to disable all
  traffic if the shared key is being abused.
- **Rate limiting:** Cloudflare Rate Limiting rules or a token-bucket in a
  Durable Object.
- **Cost attribution:** parse `usage` from Anthropic responses to bill real
  tokens per user, not just request counts.
