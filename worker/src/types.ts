// worker/src/types.ts

export type ModerationVerdict = 'CLEAN' | 'FLAGGED';

export interface ProviderConfig {
  /** Human-readable name for logging / error reporting */
  name: string;
  /** OpenAI-compatible chat completions endpoint */
  url: string;
  /** Model identifier to pass in the request body */
  model: string;
  /** Bearer token for Authorization header */
  apiKey: string;
  /** Per-request timeout in milliseconds */
  timeoutMs: number;
  /** Additional HTTP headers merged into the request */
  extraHeaders?: Record<string, string>;
  /** Additional JSON body fields merged into the payload */
  extraBody?: Record<string, unknown>;
}

export interface ModerationError {
  /** Which provider failed */
  provider: string;
  /** Category of failure */
  reason: 'timeout' | 'rate_limited' | 'http_error' | 'unparseable' | 'network_error' | 'empty_response';
  /** Optional detail string (status code, raw reply snippet, exception message) */
  detail?: string;
}

export type ChainResult =
  | { ok: true; verdict: ModerationVerdict; provider: string }
  | { ok: false; errors: ModerationError[] };
