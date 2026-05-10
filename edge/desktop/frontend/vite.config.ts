import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri's tauri.conf.json sets devUrl to http://localhost:1420.
// Build output goes to ../dist so the Tauri binary picks it up.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: "localhost",
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    target: ["es2022", "safari16"],
    sourcemap: true,
  },
});
