/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開発中（npm run dev）は、/api をこのマシンの API（python -m yanua serve --dev-login）に渡す。
// 組み立てた画面（npm run build → dist）は、API と同じサーバーから配る。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  // 社内の画面（index.html）と受付端末（kiosk.html）は別々に組み立てる。受付端末に社内の画面のコードを含めないため
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: { input: { index: "index.html", kiosk: "kiosk.html" } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
  },
});
