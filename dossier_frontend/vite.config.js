import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The frontend is served by Vite in dev. API requests go to the
// dossier app at http://localhost:8000 — we proxy them through
// Vite so the frontend can call /api/... paths without CORS.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
