import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const backend = process.env.ATLAS_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Same-origin in development too: the backend is reached through the dev server.
    // changeOrigin: false keeps the browser's Host header, so the backend's strict same-origin
    // check on cart changes (Origin must equal scheme + Host) passes through the proxy. Vite's
    // default rewrites Host to the backend address, and every cart change would get a 403.
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/healthz": { target: backend, changeOrigin: false },
    },
  },
  build: {
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
