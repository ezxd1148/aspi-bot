# Admin dashboard on EC2

The dashboard runs inside `aspi-bot.service`, sharing the bot's review lock,
publishing functions, provider tests, and reset functions. Do not start another
bot process to serve the dashboard. Existing installations keep working with the
dashboard disabled until `DASHBOARD_URL` is configured.

The public page contains only the Telegram login screen. Every dashboard page
and API request verifies the session and the user's current admin role in the
configured Telegram review group. A failed Telegram membership lookup denies
access. Login uses Telegram's current OpenID Connect flow with state, PKCE,
nonce, and RS256 signature/issuer/audience/expiry verification. Sessions expire
after 30 minutes of inactivity or 8 hours total; a service restart signs everyone
out. State-changing requests require a matching origin and CSRF token.

## 1. Point a subdomain at EC2

Choose a subdomain, such as `admin.yourdomain.com`. You do not need to transfer
the domain to AWS or buy Route 53 hosting. Change the records at whichever
provider currently manages your domain's DNS.

1. In the AWS EC2 console, select your instance and find **Public IPv4 address**.
   For a stable address, allocate an **Elastic IP** and associate it with that
   instance before creating the DNS record. AWS charges for public IPv4
   addresses; check your account's allowances. A normal auto-assigned public IP
   can change after stopping and starting the instance.
2. In your DNS provider's dashboard, add:

   | Type | Name / host | Value |
   | --- | --- | --- |
   | A | `admin` | Your EC2 public IPv4 address |

3. Remove conflicting A/AAAA records for that subdomain. Only add an AAAA record
   if you intentionally configured working IPv6 on EC2. If using Cloudflare DNS,
   start with **DNS only** while testing this direct EC2/Caddy setup.
4. Wait for DNS to update. From your computer, check:

   ```bash
   nslookup admin.yourdomain.com
   ```

The returned IPv4 address must match the EC2 address. DNS alone does not create
a working dashboard: complete the bot configuration and HTTPS setup below.

## 2. Configure Telegram login

Open the official **@BotFather** mini app, select the same moderation bot, and
open **Login Widget**. Register both Allowed URLs:

```text
https://admin.yourdomain.com
https://admin.yourdomain.com/auth/callback
```

Copy the **Client ID** and **Client Secret** into the EC2 checkout's `.env`:

```dotenv
DASHBOARD_URL=https://admin.yourdomain.com
DASHBOARD_PORT=8081
TELEGRAM_LOGIN_CLIENT_ID=your_client_id
TELEGRAM_LOGIN_CLIENT_SECRET=your_client_secret
```

These login credentials are separate from `TELEGRAM_BOT_TOKEN`. Keep Telegram's
default **RS256** signing algorithm. The requested scopes are only `openid profile`;
the dashboard does not request phone numbers or messaging permission.
Keep `.env` private and out of Git. Configure `ADMIN_GROUP_ID` as before and keep
the bot promoted to an administrator in that private review group.

## 3. Update the bot

Deploy the full updated checkout, including `src/dashboard/`, then run on EC2:

```bash
cd /home/ubuntu/aspi-bot
uv pip install --python .venv/bin/python -r src/requirements.txt
.venv/bin/python src/bot.py --check-config
sudo systemctl restart aspi-bot
sudo journalctl -u aspi-bot -n 40 --no-pager
```

`--check-config` prints the enabled dashboard URL and loopback port without
printing secrets or starting another poller. The log should report
`Dashboard listening on 127.0.0.1:8081`. The dashboard is still not exposed directly.

## 4. Set up HTTPS with Caddy

On Ubuntu EC2, install Caddy, then copy the supplied template. If Caddy already
hosts other sites, append the new site block to the existing configuration
instead of running the copy command below.

```bash
sudo apt-get update
sudo apt-get install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
```

Replace `admin.example.com` with your chosen subdomain. Keep the upstream
`127.0.0.1:8081`, or change it to match `DASHBOARD_PORT`.
Do not run another web server on the same ports.

In the instance's security group, configure inbound TCP rules:

| Port | Source | Purpose |
| --- | --- | --- |
| 443 | `0.0.0.0/0` | Public HTTPS login/dashboard |
| 80 | `0.0.0.0/0` | HTTPS redirect and certificate validation |
| 22 | Your trusted public IP with `/32` | SSH administration |

Do not add public inbound rules for 8081, 8080, or database ports. If IPv6 is
intentionally enabled, add `::/0` for 80/443 as well. Make equivalent changes to
any host firewall you already use, preserving SSH access.

Once DNS and these rules are ready:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo journalctl -u caddy -n 30 --no-pager
```

Caddy obtains and renews the HTTPS certificate and redirects HTTP. Authentication
and API authorization remain enforced by the dashboard. The supplied template
does not enable access logs, avoiding logs containing temporary OIDC callback
codes. Do not enable query-string logging or publicly serve the checkout/data folder.

## 5. Verify access and actions

1. Open the URL in a private browser window: only the login page is public.
2. Sign in as a current group administrator: the overview and review queue load.
3. Sign in as a normal Telegram user: access is denied.
4. Remove a test account's group-admin role: its existing session loses access
   on its next protected request. Keep another owner/admin account for this test.
5. Using a test publish channel, verify that a review updates Telegram and that
   a second click cannot publish the same submission again.

Provider checks run only on demand and use the same probe as `/testbots`; they
may consume API credits. Reset requires typing `RESET` and notifies the review
group. Daily reset notifications remain enabled. Next-reset time follows the
server's local timezone, matching the existing schedule; browser timestamps
display in the viewer's timezone, and daily statistics use UTC.

Attachments are listed by name/type and opened through the existing Telegram
review message. The dashboard never fetches arbitrary upload URLs. Legacy pending
entries remain reviewable but may lack a timestamp, note, or Telegram message link.

Activity metadata is stored in `data/activity.sqlite3` (or your configured data
directory), survives service restarts/resets, and is retained for 30 days with a
10,000-event cap. Confession text and attachment URLs are not copied into the
audit log. Counts begin when the dashboard is enabled; there is no invented
historical data. Provider test results are kept for the current process lifetime.

Partial media publication and process crashes are not transactional across the
Telegram API. If publishing partially fails, inspect the channel before retrying.

## Troubleshooting Telegram login

If the callback displays **Could not verify Telegram login**, update the checkout,
restart `aspi-bot`, and attempt a fresh login from the public login page. Then run:

```bash
sudo journalctl -u aspi-bot --since "5 minutes ago" --no-pager --grep="Dashboard Telegram login failed"
```

The diagnostic reports the failed stage and a safe reason, for example:

```text
Dashboard Telegram login failed: stage=token_exchange reason=token_endpoint_http_401_invalid_client
```

| Reason | What to check |
| --- | --- |
| `token_endpoint_http_401_invalid_client` | Copy the Login Widget Client ID and Client Secret from the same bot in BotFather into `.env`, then restart. These are separate from the bot token. |
| `token_endpoint_http_400_invalid_grant` | Start a fresh login; confirm the exact HTTPS callback URI matches `DASHBOARD_URL` plus `/auth/callback`. Codes expire and cannot be reused. |
| `unsupported_signing_algorithm_set_botfather_RS256` | Select RS256 under BotFather's Login Widget advanced settings. |
| `client_id_mismatch` | Confirm the Client ID belongs to the bot used for this login. |
| `token_expired`, `token_not_yet_valid_check_server_clock`, `login_token_too_old` | Check EC2's clock with `timedatectl status`, then start a fresh login. |
| `telegram_connection_failed`, `telegram_request_timed_out`, `signing_keys_http_...` | Check outbound HTTPS connectivity from EC2 to `oauth.telegram.org`. |
| `missing_claim_nonce`, `nonce_mismatch`, `invalid_signature`, or other verification failures | Share only the diagnostic line for investigation. Verification still denies access. |

Diagnostics omit credentials, callback codes, tokens, profile claims, and provider
error descriptions. Do not share your `.env`, a full callback URL, or ID token.

The dashboard uses a redirect flow and external local JavaScript; it does not
require inline scripts. A browser CSP warning alone does not identify this
server-side verification failure. Keep `script-src 'self'` while diagnosing it.

## Rollback

Clear `DASHBOARD_URL` and restart `aspi-bot.service` to disable the dashboard.
Keep the existing pending/processed files and SQLite audit log. No migration or
separate database service is needed. Remove the Caddy site if you no longer want
to expose a login page.

## Local verification

```bash
python -m unittest discover -s tests -v
node --check src/dashboard/app.js
```

The unit/integration tests use local HTTP servers, generated signing keys, and
mocked Telegram/Tally/provider APIs. They do not send real confessions or delete
live submissions. The optional `tests/browser_dashboard.py` check additionally
requires Playwright and an installed Chromium executable; it uses a temporary
HTTPS server and mocked APIs to exercise desktop/mobile pages and admin actions.

References: [Telegram Login](https://core.telegram.org/bots/telegram-login),
[AWS security-group web rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html),
[AWS Elastic IP addresses](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html),
[Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).
