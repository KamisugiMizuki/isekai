import { defineConfig } from "vite";

// dev 端口固定 1420：与 tauri.conf.json 的 devUrl 一致（改一处必须改另一处）
export default defineConfig({
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
  },
  build: {
    target: "chrome110",
    outDir: "dist",
    emptyOutDir: true,
  },
});
