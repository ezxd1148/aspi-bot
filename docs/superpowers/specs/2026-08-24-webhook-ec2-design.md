# Webhook setup for EC2 (Cloudflare Tunnel) — Design

**Date:** 2026-08-24
**Status:** Approved (design), not yet implemented
**Branch:** `feature/webhook-ec2`

## Goal

Move aspi-bot from long-polling + Tally polling to a push-based webhook
architecture on EC2, with inbound traffic fronted by a **Cloudflare Tunnel**.
Two independent inbound channels are replaced:

| Channel | Before | After |
|---|---|---|
| Tally → bot (new confessions) | `check_tally` polls every `POLL_INTERVAL_SECONDS` | Tally webhook POSTs each submission to `/tally` |
| Telegram → bot (`/start`, `/stats`, `/reset`, Approve/Reject buttons) | `run_polling` long-polls Telegram | Telegram webhook POSTs updates to `/telegram` |

A single local aiohttp app serves both, plus a `/health` probe. TLS is
terminated by Cloudflare, so the bot only listens on plain HTTP localhost.

## Constraints (carried from prior session)

- The bot must **never** be executed by Claude during this work. Validate only
  via `py_compile` and unit tests. Use `.env` for test IDs.
- Changes stay **uncommitted** until the user confirms testing — however, this
  spec document is committed on `feature/webhook-ec2` as the brainstorming
  skill requires.
- Routing rule preserved: CLEAN auto-publishes; FLAGGED and UNSURE both escalate
  to admin. Nothing silently discarded. (Untouched by this change.)

## Transport architecture

```
Telegram ──▶ https://yourdomain.com/telegram ─┐
Tally    ──▶ https://yourdomain.com/tally    ─┼─▶ cloudflared ─▶ http://localhost:PORT/{path}
Probe   ──▶ https://yourdomain.com/health    ─┘
```

- Cloudflare terminates TLS at the edge (standard 443). The bot listens on a
  plain-HTTP local port (no cert files, no 443/80/88/8443 restriction).
- `cloudflared` config forwards `hostname: yourdomain.com → http://localhost:PORT`
  and returns `404` for unmatched paths.
- Because TLS is Cloudflare's, we do **not** use PTB's integrated
  `run_webhook` TLS (cert/key) path. We mount PTB's webhook handler onto our own
  aiohttp `web_app`.

## Components

### 1. `src/bot.py` — unified startup

Add a `MODE` env toggle:

- `MODE=polling` (default): existing `run_polling` behavior. Also calls
  `await bot.delete_webhook()` at startup so a stale webhook cannot steal
  updates from the poller.
- `MODE=webhook`: build an aiohttp `web_app`, attach:
  - `application.add_webhook_handler(web_app, "/telegram")` (PTB v22 API)
  - a plain `GET /health` handler → `200 "OK"`
  - a `POST /tally` handler (see §2)
  - `web_app.on_startup`: `await application.initialize()`,
    `await application.start()`,
    `await application.bot.set_webhook(url=WEBHOOK_URL + "/telegram", secret_token=WEBHOOK_SECRET)`
  - `web_app.on_cleanup`:
    `await application.bot.delete_webhook()`,
    `await application.stop()`, `await application.shutdown()`
  - `web.run_app(web_app, host="127.0.0.1", port=PORT)`

Command handlers (`/start`, `/stats`, `/reset`), `CallbackQueryHandler`,
error handler, `button_handler`, `daily_reset`, and `_seed_tracker` are unchanged.

`check_tally` is **removed** from the JobQueue in webhook mode. It remains
available only as the cold-start seeding path used by `_seed_tracker` (via
`fetch_data`) — see §4.

### 2. `POST /tally` — Tally webhook receiver

- Tally is configured (in the Tally dashboard) to POST each form submission to
  `https://yourdomain.com/tally?secret=TALLY_WEBHOOK_SECRET`.
- Handler flow:
  1. Reject with `403` if `request.query.get("secret") != TALLY_WEBHOOK_SECRET`.
  2. Parse JSON body via `lib.fetch_form.parse_webhook(payload)` →
     `(sid, text, files)` (same shape as `extract_submission`).
  3. Malformed/incomplete → `400`, log, return.
  4. If `lib.tracker.is_processed(sid)` → `200` (already handled, idempotent).
  5. Else `application.create_task(_handle_submission(application, sid, text, files))`
     and return `200`.
- Tally webhooks are at-least-once; the `tracker` dedup guarantees no
  double-publish.
- Reuses the existing `_handle_submission` / moderation / routing pipeline
  unchanged.

### 3. `src/lib/fetch_form.py` — add `parse_webhook`

- New function `parse_webhook(payload: dict) -> tuple[str|None, str|None, list[dict]]`
  mirroring `extract_submission`'s output contract, but reading Tally's webhook
  payload schema (field IDs from the configured form) instead of the
  submissions-GET schema.
- `fetch_data` and `extract_submission` remain for cold-start seeding.

### 4. Cold-start seeding preserved

`_seed_tracker()` keeps calling `fetch_data(...)` to pre-mark existing Tally
submission IDs, so flipping to the webhook doesn't re-publish the backlog.
This is the only remaining use of `fetch_data`/`extract_submission`.

### 5. `src/lib/web_server.py` — retired

The existing health-check-only server is replaced by the `/health` route inside
the unified aiohttp app. The file is deleted; `lib.web_server.start()` call in
`bot.py` is removed.

## New env vars

| Var | Default | Purpose |
|---|---|---|
| `MODE` | `polling` | `webhook` for EC2 prod, `polling` for local testing |
| `WEBHOOK_URL` | — | Public base URL, e.g. `https://yourdomain.com` |
| `WEBHOOK_SECRET` | — | Telegram secret-token; validated on `X-Telegram-Bot-Api-Secret-Token`, blocks spoofed updates |
| `TALLY_WEBHOOK_SECRET` | — | Shared secret in `/tally?secret=`; validates Tally calls |
| `PORT` | `8080` | Unified local HTTP port (already present; now the server port) |

`.env.example` gains the four new keys (documented, blank). The existing
`OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` (and the 7 provider keys in the real
`.env`) are untouched — AI moderation is out of scope for this change.

## cloudflared configuration (operational, not in repo)

- Tunnel `ingress`:
  - `hostname: yourdomain.com` → `http://localhost:PORT`
  - `http_status: 404` (catch-all)
- Tally dashboard: form webhook → `https://yourdomain.com/tally?secret=<TALLY_WEBHOOK_SECRET>`

## Error handling

- Tally bad/missing secret → `403`.
- Tally malformed payload → `400` + log.
- Telegram secret mismatch → PTB rejects the update automatically.
- Tally at-least-once delivery → deduped by `tracker`.
- If `set_webhook` fails at startup (e.g. `WEBHOOK_URL` unset in webhook mode) →
  log a clear error and exit; never silently fall back to polling while a
  webhook may be half-configured.

## Testing (no bot execution)

- `py_compile src/bot.py src/lib/fetch_form.py` — must pass.
- New unit tests (no network, no Telegram):
  - `lib.fetch_form.parse_webhook` maps a sample Tally webhook payload to the
    expected `(sid, text, files)` shape.
  - `parse_webhook` returns empty text/files safe when fields absent.
  - Aiohttp test client: `GET /health` → `200`; `POST /tally` without secret →
    `403`; `POST /tally` with wrong secret → `403`.
- Existing 20-test suite must continue to pass (`test_moderation.py`,
  `fetch_state`, `metrics`).
- Manual (user-run, not Claude): set `MODE=polling` with a TEST channel +
  test `ADMIN_CHAT_ID` to validate handler wiring locally; `MODE=webhook` only
  on EC2 with the tunnel up.

## Out of scope

- AI moderation chain (11 text + 4 vision APIs) — unchanged.
- Metrics semantics, incremental-fetch cursor logic (only the polling trigger
  changes, not the data model).
- Two-instance / token-collision safeguards (separate operational concern).

## Implementation order (for the writing-plans step)

1. `fetch_form.parse_webhook` + tests (no bot changes).
2. `/tally` + `/health` routes + `bot.py` unified startup + `MODE` toggle.
3. Retire `web_server.py`; remove its call.
4. `.env.example` new keys; `py_compile` + full test run.
5. cloudflared + Tally dashboard operational steps (documented, user-run).
