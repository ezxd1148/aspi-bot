// worker/test/moderation-chain.test.ts

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { moderateText } from '../src/moderation/chain';
import { ProviderConfig } from '../src/types';

const SYSTEM_PROMPT = 'You are a content moderator. Output only CLEAN or FLAGGED.';

/** Build a mock Response with a JSON body. */
function mockJsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

/** Minimal valid provider for testing. */
function makeProvider(overrides: Partial<ProviderConfig> = {}): ProviderConfig {
  return {
    name: 'test-provider',
    url: 'https://test.example/v1/chat/completions',
    model: 'test-model',
    apiKey: 'test-key',
    timeoutMs: 5000,
    ...overrides,
  };
}

describe('moderateText', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // --- Success paths ---

  it('returns clean verdict from the first provider', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] })
    ));

    const result = await moderateText('hello world', SYSTEM_PROMPT, [makeProvider({ name: 'p1' })]);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.verdict).toBe('CLEAN');
      expect(result.provider).toBe('p1');
    }
  });

  it('returns flagged verdict from the first provider', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockJsonResponse({ choices: [{ message: { content: 'FLAGGED' } }] })
    ));

    const result = await moderateText('bad stuff', SYSTEM_PROMPT, [makeProvider({ name: 'p1' })]);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.verdict).toBe('FLAGGED');
      expect(result.provider).toBe('p1');
    }
  });

  it('is case-insensitive when parsing verdict', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockJsonResponse({ choices: [{ message: { content: '  clean  ' } }] })
    ));

    const result = await moderateText('hello', SYSTEM_PROMPT, [makeProvider()]);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.verdict).toBe('CLEAN');
    }
  });

  it('extracts verdict from reasoning_content when content is empty', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockJsonResponse({
        choices: [{
          message: {
            content: '',
            reasoning_content: 'Some reasoning here\nCLEAN',
          },
        }],
      })
    ));

    const result = await moderateText('hello', SYSTEM_PROMPT, [makeProvider()]);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.verdict).toBe('CLEAN');
    }
  });

  // --- Fallback paths ---

  it('falls back to next provider on 429 rate limit', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({}, 429))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'FLAGGED' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'rate-limited' }),
      makeProvider({ name: 'backup' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('backup');
    }
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('falls back to next provider on 402 no credits', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({}, 402))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'no-credits' }),
      makeProvider({ name: 'backup' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('backup');
    }
  });

  it('falls back to next provider on 500 error', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({}, 500))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'crashing' }),
      makeProvider({ name: 'stable' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('stable');
    }
  });

  it('falls back on unparseable response (no CLEAN/FLAGGED keyword)', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({
        choices: [{ message: { content: 'This text is probably fine but I am not sure.' } }],
      }))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'vague' }),
      makeProvider({ name: 'precise' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('precise');
    }
  });

  it('falls back on empty response content', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: '' } }] }))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'empty' }),
      makeProvider({ name: 'works' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('works');
    }
  });

  it('falls back on timeout', async () => {
    vi.useFakeTimers();

    // First provider: never resolves, only rejects on abort
    const mockFetch = vi.fn()
      .mockImplementationOnce((_url: string, opts: RequestInit) => {
        return new Promise<Response>((_, reject) => {
          opts.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted', 'AbortError'));
          });
        });
      })
      .mockResolvedValueOnce(
        mockJsonResponse({ choices: [{ message: { content: 'CLEAN' } }] })
      );
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'slow', timeoutMs: 5000 }),
      makeProvider({ name: 'fast', timeoutMs: 5000 }),
    ];

    const resultPromise = moderateText('text', SYSTEM_PROMPT, providers);

    // Advance time past the first provider's 5s timeout
    await vi.advanceTimersByTimeAsync(5000);
    const result = await resultPromise;

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('fast');
    }
  });

  it('falls back on network error', async () => {
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new Error('Connection refused'))
      .mockResolvedValueOnce(mockJsonResponse({ choices: [{ message: { content: 'FLAGGED' } }] }));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'offline' }),
      makeProvider({ name: 'online' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('online');
    }
  });

  // --- Exhaustion path ---

  it('returns all errors when every provider fails', async () => {
    const mockFetch = vi.fn()
      .mockResolvedValueOnce(mockJsonResponse({}, 429))
      .mockResolvedValueOnce(mockJsonResponse({}, 500))
      .mockRejectedValueOnce(new Error('Network down'));
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'p1' }),
      makeProvider({ name: 'p2' }),
      makeProvider({ name: 'p3' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors).toHaveLength(3);
      expect(result.errors[0]).toMatchObject({ provider: 'p1', reason: 'rate_limited' });
      expect(result.errors[1]).toMatchObject({ provider: 'p2', reason: 'http_error' });
      expect(result.errors[2]).toMatchObject({ provider: 'p3', reason: 'network_error' });
    }
  });

  // --- Edge cases ---

  it('stops at first valid verdict and does not call remaining providers', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      mockJsonResponse({ choices: [{ message: { content: 'FLAGGED' } }] })
    );
    vi.stubGlobal('fetch', mockFetch);

    const providers = [
      makeProvider({ name: 'first' }),
      makeProvider({ name: 'second' }),
      makeProvider({ name: 'third' }),
    ];

    const result = await moderateText('text', SYSTEM_PROMPT, providers);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.provider).toBe('first');
    }
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('returns error when no providers configured', async () => {
    const result = await moderateText('text', SYSTEM_PROMPT, []);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors).toHaveLength(1);
      expect(result.errors[0].reason).toBe('network_error');
    }
  });
});
