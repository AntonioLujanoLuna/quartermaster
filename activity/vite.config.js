import { defineConfig } from "vite";

// Everything the client fetches goes through Discord's proxy under `/.proxy/`,
// which is why the app calls `/.proxy/api/...` rather than `/api/...`. In the
// dev server there is no proxy, so that prefix is stripped and forwarded to the
// API running locally — the same paths work in both places, and no code has to
// know which one it is in.
export default defineConfig({
  envDir: "..",
  server: {
    port: 5173,
    proxy: {
      "/.proxy/api": {
        target: process.env.QM_API_URL || "http://127.0.0.1:8080",
        changeOrigin: true,
        // The live feed upgrades on this same prefix, so the dev server has to
        // carry the upgrade too or the screen loads and never moves.
        ws: true,
        rewrite: (path) => path.replace(/^\/\.proxy/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
