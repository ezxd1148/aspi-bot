# aspi-bot

Admin command: `/testbots` tests OpenRouter, Groq, NVIDIA, and DeepSeek separately
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
