import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the store's API runs separately (python -m intentguard.storefront).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8766" },
  },
});
