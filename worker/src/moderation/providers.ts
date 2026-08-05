// worker/src/moderation/providers.ts

import { ProviderConfig } from '../types';

/**
 * Environment variables expected by the provider chain.
 * Mirrors the Python project's env vars + new CF/Groq/Google keys.
 */
export interface ModerationEnv {
  CF_ACCOUNT_ID?: string;
  CF_API_TOKEN?: string;
  GROQ_API_KEY?: string;
  GOOGLE_AI_API_KEY?: string;
  OPENROUTER_API_KEY?: string;
  NVIDIA_API_KEY?: string;
}

/**
 * Build the ordered provider list from available env vars.
 * Providers without credentials are silently skipped.
 * Order per spec: CF AI → Groq → Google → OpenRouter → NIM.
 */
export function getProviders(env: ModerationEnv): ProviderConfig[] {
  const providers: ProviderConfig[] = [];

  // 1. Cloudflare Workers AI (same edge network, lowest latency)
  if (env.CF_ACCOUNT_ID && env.CF_API_TOKEN) {
    providers.push({
      name: 'cloudflare',
      url: `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/v1/chat/completions`,
      model: '@cf/meta/llama-3.2-3b-instruct',
      apiKey: env.CF_API_TOKEN,
      timeoutMs: 5000,
    });
  }

  // 2. Groq (ultra-fast inference)
  if (env.GROQ_API_KEY) {
    providers.push({
      name: 'groq',
      url: 'https://api.groq.com/openai/v1/chat/completions',
      model: 'llama-3.1-8b-instant',
      apiKey: env.GROQ_API_KEY,
      timeoutMs: 5000,
    });
  }

  // 3. Google AI Studio (Gemini Flash — generous 1,500/day limit)
  if (env.GOOGLE_AI_API_KEY) {
    providers.push({
      name: 'google',
      url: 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
      model: 'gemini-2.0-flash',
      apiKey: env.GOOGLE_AI_API_KEY,
      timeoutMs: 5000,
    });
  }

  // 4. OpenRouter (free tier, 20 RPM hard cap)
  if (env.OPENROUTER_API_KEY) {
    providers.push({
      name: 'openrouter',
      url: 'https://openrouter.ai/api/v1/chat/completions',
      model: 'poolside/laguna-xs-2.1:free',
      apiKey: env.OPENROUTER_API_KEY,
      timeoutMs: 5000,
      extraHeaders: {
        'HTTP-Referer': 'https://github.com/aspi-bot',
        'X-Title': 'aspi-bot',
      },
      extraBody: { reasoning_effort: 'none' },
    });
  }

  // 5. NVIDIA NIM (~40 RPM, workhorse last resort)
  if (env.NVIDIA_API_KEY) {
    providers.push({
      name: 'nvidia',
      url: 'https://integrate.api.nvidia.com/v1/chat/completions',
      model: 'mistralai/mistral-medium-3.5-128b',
      apiKey: env.NVIDIA_API_KEY,
      timeoutMs: 5000,
      extraBody: { reasoning_effort: 'none' },
    });
  }

  return providers;
}
