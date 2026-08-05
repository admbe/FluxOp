import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // The app shell previously bundled Recharts into the ~820 kB entry
        // chunk. Splitting it (819 -> 404 kB raw entry, 244 -> 124 kB gzip)
        // lets the browser cache the charting vendor across releases and
        // shrinks what an app change actually invalidates. React stays in
        // the entry: every route needs it on first paint anyway, and
        // mapping it out only produced an empty stub chunk.
        manualChunks: {
          recharts: ["recharts"],
        },
      },
    },
  },
});
