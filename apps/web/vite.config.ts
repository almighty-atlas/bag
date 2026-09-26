import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

// In development the API runs on :8000; in production nginx proxies /api instead.
export default defineConfig({
  plugins: [preact()],
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { sourcemap: false, target: "es2022" },
});
