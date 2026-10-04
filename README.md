# aspi-bot

Shared admin review:

1. Create a private Telegram group and invite your reviewers and the bot.
2. Promote the bot and your reviewers to group administrators.
3. Send `/start@YourBotUsername` in the group to get the group's chat ID.
4. Set `ADMIN_GROUP_ID` in EC2's `.env` to that group ID, usually a negative number.
5. Deploy the updated code and run `sudo systemctl restart aspi-bot`.

Flagged submissions and their attachments go to that group. The group's owner
and current administrators can approve/reject and run `/status`, `/testbots`, and `/reset`.
Ordinary members and people in other chats cannot perform those actions.
Use `/testbots@YourBotUsername` if several bots are in the group. Send commands
as yourself rather than as an anonymous administrator. Keep the bot authorized
to send messages and files in the group and publish in the confession channel.
The bot must be an administrator to reliably verify other users' roles, per
[Telegram's getChatMember documentation](https://core.telegram.org/bots/api#getchatmember).

Completed review messages name the reviewer and remove the decision buttons.
Concurrent clicks are serialized, and a failed message edit cannot publish the
same submission again. Run only one bot process per token. Decisions are not
transactional across a process crash or a partially completed media broadcast.
Finish outstanding reviews in the old chat before changing the review destination;
old review buttons will no longer be authorized once the destination changes.
A positive private-chat `ADMIN_CHAT_ID` retains single-admin operation only when
`ADMIN_GROUP_ID` is absent. An explicit group always takes priority, and an invalid
or inaccessible group stops startup instead of silently falling back to a DM.

Configuration is loaded once from `.env` in this checkout (beside README.md),
regardless of the shell's working directory. Values in that file override inherited
environment values. Duplicate review-destination entries are rejected. Restart
after editing the file. `/start` shows the active destination and build fingerprint;
`/status` additionally shows the checkout/config paths and verifies group access.
It lists the detected human owner/admins with names, usernames (when available),
Telegram IDs, and roles, and identifies the admin who requested the status.
Startup checks the group type, privacy, and bot administrator role before polling.
Registered handlers and Telegram's command menu come from the same command list.

Verify the installed configuration on EC2 without starting another polling process:

```bash
cd /home/ubuntu/aspi-bot
.venv/bin/python src/bot.py --check-config
sudo systemctl restart aspi-bot
sudo journalctl -u aspi-bot -n 30 --no-pager
```

Then run `/status@YourBotUsername` in the review group. Compare its build and
destination to the command-line output. `/help` lists every command. Unknown
commands receive a reply, while commands addressed to another bot are ignored.
Provider test failures are reported individually; a failed Tally reset preserves
local pending reviews instead of reporting a successful reset. External services,
permissions, credentials, and duplicate bot processes can still cause live failures.

The scheduled daily reset sends its start and completion (or failure) notifications
to the configured admin review chat. If Telegram cannot deliver a notification,
the bot logs that failure and continues the reset.

If logs show `Handler error: Conflict`, another polling process or a competing
webhook deployment is using the same Telegram token. Keep one polling instance.
Check both `aspi-bot` and `aspi-bot-ec2` services, manual Python sessions, your
local development machine, and other servers. Stopping a duplicate process fixes
this conflict; editing the review group ID does not. The error handler reports
polling and webhook conflicts separately without logging the token.

Borderline flags receive a short **Review note** explaining the rule/word or
context that needs human judgment. Obvious violations omit the note. This uses
one additional request to the same configured provider after a valid FLAGGED
classification; clean submissions do not need that extra request. A failed note
request leaves the submission pending without an explanation. AI notes are
advisory and may be wrong; only the classification controls automatic posting.

Admin command: `/testbots` tests OpenRouter, Groq, NVIDIA, and DeepSeek separately
using the loaded moderation prompt and configured models. Run it in the chat
configured by `ADMIN_GROUP_ID` (or legacy `ADMIN_CHAT_ID`) after restarting the bot.

Each configured provider receives a harmless sample (expected `CLEAN`) and a
disguised restricted-topic sample (expected `FLAGGED`). Results show each check,
elapsed time, missing API keys, authentication errors, rate limits, timeouts,
and invalid responses. Requests run off the Telegram event loop; providers are
tested concurrently without falling back to another provider. A test can take
about two minutes and consumes provider quota or credits. Test submissions are
never broadcast or stored as confessions. Passing these samples verifies basic
connectivity and classification, not every moderation rule.

Both checks must pass for a provider to pass the basic probe. Failure details
distinguish error bodies inside HTTP 200 responses, invalid JSON, missing/empty
choices, empty final answers, refusals, content filtering, and token limits.
Raw provider responses and error messages are not included in Telegram replies.
To investigate a failed OpenRouter probe, deploy the updated code, restart the
service, run `/testbots`, and compare the two requests with your OpenRouter
Activity dashboard. Repeated passes and a broader set of labeled moderation
examples are needed to evaluate reliability beyond these two samples.

Moderation fallback order: OpenRouter -> Groq -> NVIDIA -> DeepSeek. Providers
without a configured API key are skipped; HTTP failures and rate limits move to
the next provider.

To enable Groq, create an API key at [Groq Console](https://console.groq.com/keys)
and add `GROQ_API_KEY=your_key_here` to `.env`, then restart the bot and run
`/testbots`. The bot uses `openai/gpt-oss-120b` with low reasoning effort and
reasoning excluded from the response, through Groq's OpenAI-compatible API.
No additional SDK is required.

Groq's [Free plan limits](https://console.groq.com/docs/rate-limits) currently
list this model at 30 requests/minute, 1,000 requests/day, 8,000 tokens/minute,
and 200,000 tokens/day. Limits include the moderation prompt on each request
and apply across the organization. Your account's exact limits may differ.
Use a Free-plan account for free access; this integration does not change your
account's billing plan or enforce a spending cap.

References: [supported models](https://console.groq.com/docs/models),
[API compatibility](https://console.groq.com/docs/openai), and
[reasoning parameters](https://console.groq.com/docs/reasoning).
