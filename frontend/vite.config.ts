import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/financial-reports": "http://127.0.0.1:8765",
    },
  },
  build: {
    outDir: "../src/ai_accounting/static/dashboard",
    emptyOutDir: true,
  },
});
