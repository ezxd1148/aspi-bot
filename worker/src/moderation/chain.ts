// worker/src/moderation/chain.ts

import { ProviderConfig, ChainResult, ModerationError } from '../types';

/**
 * Run the moderation fallback chain.
 *
 * Tries each provider in order. On the first parseable CLEAN or FLAGGED
 * response, returns immediately. On any failure (429, timeout, 5xx,
 * unparseable response, network error), records the error and tries the
 * next provider.
 *
 * If no providers are configured, returns an error immediately.
 * If all providers fail, returns the full error list for the caller
 * to handle via the §6 failure path (failed_id lock + admin notification).
 */
export async function moderateText(
  text: string,
  systemPrompt: string,
  providers: ProviderConfig[],
): Promise<ChainResult> {
  if (providers.length === 0) {
    return {
      ok: false,
      errors: [{ provider: 'none', reason: 'network_error', detail: 'No moderation providers configured' }],
    };
  }

  const errors: ModerationError[] = [];

  for (const provider of providers) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), provider.timeoutMs);

    try {
      const headers: Record<string, string> = {
        'Authorization': `Bearer ${provider.apiKey}`,
        'Content-Type': 'application/json',
        ...provider.extraHeaders,
      };

      const body: Record<string, unknown> = {
        model: provider.model,
        messages: [
          { role: 'system', content: systemPrompt },
          {
            role: 'user',
            content: `Classify as CLEAN or FLAGGED. Reply with only that word:\n\n${text}`,
          },
        ],
        max_tokens: 500,
        temperature: 0.1,
        ...provider.extraBody,
      };

      const response = await fetch(provider.url, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      // Rate-limit or no-credits → fast skip to next provider
      if (response.status === 429 || response.status === 402) {
        errors.push({
          provider: provider.name,
          reason: 'rate_limited',
          detail: `HTTP ${response.status}`,
        });
        continue;
      }

      // Any other non-2xx → skip to next provider
      if (!response.ok) {
        errors.push({
          provider: provider.name,
          reason: 'http_error',
          detail: `HTTP ${response.status}`,
        });
        continue;
      }

      // Parse the response
      const data = (await response.json()) as {
        choices?: Array<{ message?: { content?: string; reasoning_content?: string } }>;
      };

      let reply = (data?.choices?.[0]?.message?.content ?? '').trim().toUpperCase();

      // Fallback: some reasoning models put the answer in reasoning_content
      if (!reply) {
        const reasoning = (data?.choices?.[0]?.message?.reasoning_content ?? '').trim();
        if (reasoning) {
          reply = reasoning.toUpperCase().split('\n').pop()!.trim();
        }
      }

      if (!reply) {
        errors.push({
          provider: provider.name,
          reason: 'empty_response',
        });
        continue;
      }

      if (reply.includes('CLEAN')) {
        return { ok: true, verdict: 'CLEAN', provider: provider.name };
      }

      if (reply.includes('FLAGGED')) {
        return { ok: true, verdict: 'FLAGGED', provider: provider.name };
      }

      // Response was parseable but contained neither keyword
      errors.push({
        provider: provider.name,
        reason: 'unparseable',
        detail: reply.slice(0, 200),
      });
    } catch (err: unknown) {
      clearTimeout(timeoutId);

      if (err instanceof DOMException && err.name === 'AbortError') {
        errors.push({
          provider: provider.name,
          reason: 'timeout',
          detail: `Exceeded ${provider.timeoutMs}ms`,
        });
      } else {
        errors.push({
          provider: provider.name,
          reason: 'network_error',
          detail: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }

  return { ok: false, errors };
}
