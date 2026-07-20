import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    sourcemap: false,
    target: "es2024",
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.js",
  },
});
