import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served by the gateway at /console in production; in development the owner API is proxied to it.
export default defineConfig({
  base: "/console/",
  build: { chunkSizeWarningLimit: 1100 }, // AG Grid core is most of the bundle
  plugins: [react()],
  server: {
    port: 5174,
    proxy: { "/owner": "http://127.0.0.1:8765" },
  },
});
