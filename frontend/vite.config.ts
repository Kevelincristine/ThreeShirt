import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const djangoTarget = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: djangoTarget,
        changeOrigin: true,
      },
      "/proposals": {
        target: djangoTarget,
        changeOrigin: true,
      },
    },
  },
});
