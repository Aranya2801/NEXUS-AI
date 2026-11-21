import { defineConfig } from "vitest/config";

// Contract tests run in Node against a LIVE backend (NEXUS_API_URL); they are not part of `npm test`.
export default defineConfig({
  test: { environment: "node", globals: true, include: ["src/**/*.contract.ts"], testTimeout: 60_000, hookTimeout: 60_000 },
});
