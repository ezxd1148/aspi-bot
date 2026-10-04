# aspi-bot

Admin command: `/testbots` tests OpenRouter, NVIDIA, and DeepSeek separately
using the loaded moderation prompt and configured models. Run it in the chat
configured by `ADMIN_CHAT_ID` after restarting the bot with the updated code.

Each configured provider receives a harmless sample (expected `CLEAN`) and a
disguised restricted-topic sample (expected `FLAGGED`). Results show each check,
elapsed time, missing API keys, authentication errors, rate limits, timeouts,
and invalid responses. Requests run off the Telegram event loop; providers are
tested concurrently without falling back to another provider. A test can take
about two minutes and consumes provider quota or credits. Test submissions are
never broadcast or stored as confessions. Passing these samples verifies basic
connectivity and classification, not every moderation rule.
