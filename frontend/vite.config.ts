import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API is proxied under /api so the browser sees one origin (no CORS, JWT never leaves the origin).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.NEXUS_API_URL ?? "http://127.0.0.1:8000", changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  build: { sourcemap: true, target: "es2022" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
