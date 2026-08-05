# aspi-bot Cloudflare Worker — Setup Guide

## What's implemented vs not

| Done | Not yet |
|------|---------|
| 5-provider moderation chain | Tally webhook parsing |
| Health check endpoint (`GET /`) | R2 lock system (§5) |
| Webhook stub (`POST /` returns 200) | Telegram posting |
| 14 passing tests | Admin approve/reject flow |
| wrangler build verified | `failed_id` crash recovery |

The moderation chain is production-ready. The webhook handler, lock system, and Telegram integration will be built in future plans.

---

## Step 1: Prerequisites

### Cloudflare account
1. Sign up at https://dash.cloudflare.com/sign-up (free tier: 100k requests/day, 10ms CPU per request)
2. Install wrangler CLI: `npm install -g wrangler`
3. Login: `wrangler login`

### API keys you need (get at least one)

| Provider | Free limit | Sign up |
|----------|-----------|---------|
| **Cloudflare Workers AI** | 10k/mo | Already included with CF account |
| **Groq** | 1,000/day | https://console.groq.com |
| **Google AI Studio** | 1,500/day | https://aistudio.google.com/apikey |
| **OpenRouter** | 20 RPM / 50-1000/day | https://openrouter.ai/keys |
| **NVIDIA NIM** | ~40 RPM | https://build.nvidia.com (developer account) |

You only need ONE provider configured for the bot to work. More providers = more resilient. Order of priority: CF AI → Groq → Google → OpenRouter → NIM.

---

## Step 2: Configure secrets

From the `worker/` directory, run these for each provider you're using:

```bash
# Cloudflare Workers AI (requires account ID + API token)
wrangler secret put CF_ACCOUNT_ID     # Found at: dash.cloudflare.com → your account → copy Account ID
wrangler secret put CF_API_TOKEN      # Create at: dash.cloudflare.com → API Tokens → Create Token → "Workers AI" template

# Groq
wrangler secret put GROQ_API_KEY      # From console.groq.com → API Keys

# Google AI Studio
wrangler secret put GOOGLE_AI_API_KEY # From aistudio.google.com/apikey

# OpenRouter
wrangler secret put OPENROUTER_API_KEY # From openrouter.ai/keys

# NVIDIA NIM
wrangler secret put NVIDIA_API_KEY    # From build.nvidia.com → API Keys
```

Verify secrets are set:
```bash
wrangler secret list
```

---

## Step 3: Deploy

```bash
cd worker
npm install --legacy-peer-deps
npx vitest run          # 14 tests should pass
npx wrangler deploy     # Deploy to Cloudflare
```

After deploy you'll get a URL like `https://aspi-bot.<your-subdomain>.workers.dev`.

---

## Step 4: Verify it works

```bash
# Health check — shows configured providers
curl https://aspi-bot.<your-subdomain>.workers.dev/

# Should return something like:
# aspi-bot worker running. 3 moderation provider(s) configured.
#   - groq: llama-3.1-8b-instant
#   - google: gemini-2.0-flash
#   - openrouter: poolside/laguna-xs-2.1:free
```

---

## Step 5: Connect Tally webhook (future)

Once the full webhook handler is built:

1. Go to your Tally form → Settings → Integrations → Webhooks
2. Set webhook URL to `https://aspi-bot.<your-subdomain>.workers.dev/`
3. Tally will POST each submission as JSON
4. Enable webhook signing (SHA256) — verify key in a future step

For now, the Worker just returns 200 to any POST (stub).

---

## Step 6: What still needs to be built

The moderation chain is done. Remaining per the migration plan:

1. **§5 — R2 lock system:** Atomic `pending/{submissionId}` writes to prevent duplicate processing
2. **Tally webhook parsing:** Extract `submissionId`, text, and file attachments from webhook payload
3. **Telegram integration:** Post clean submissions to channel, DM admin for flagged
4. **Admin approve/reject:** Inline button handling for manual review
5. **`ctx.waitUntil()` wiring:** Return 200 immediately, moderate async
6. **Crash recovery:** TTL on `processing` lock state

Run `npx wrangler tail` to stream live logs from the deployed Worker.
