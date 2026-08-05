// worker/src/index.ts

import { moderateText } from './moderation/chain';
import { getProviders, ModerationEnv } from './moderation/providers';

// SYSTEM_PROMPT.md imported as a raw string
// @ts-expect-error — .md import handled by wrangler
import systemPrompt from './moderation/SYSTEM_PROMPT.md';

export interface Env extends ModerationEnv {
  // Future: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_CHAT_ID, etc.
  // Future: STATE R2 bucket binding
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Stub: full webhook handler will be implemented in a future plan.
    // For now this validates the moderation chain compiles, providers load,
    // and a smoke-test request works.

    const url = new URL(request.url);

    // GET / — health check
    if (request.method === 'GET' && url.pathname === '/') {
      const providers = getProviders(env);
      return new Response(
        `aspi-bot worker running. ${providers.length} moderation provider(s) configured.\n` +
        providers.map(p => `  - ${p.name}: ${p.model}`).join('\n'),
        { status: 200, headers: { 'Content-Type': 'text/plain' } },
      );
    }

    // POST / — webhook stub
    if (request.method === 'POST' && url.pathname === '/') {
      const providers = getProviders(env);

      if (providers.length === 0) {
        return new Response('No moderation providers configured', { status: 500 });
      }

      // Future: parse Tally webhook, extract text, atomic lock, moderate, route
      return new Response('OK', { status: 200 });
    }

    return new Response('Not Found', { status: 404 });
  },
};
