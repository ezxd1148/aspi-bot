// worker/vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // happy-dom provides DOMException for AbortError testing
    environment: 'happy-dom',
  },
});
