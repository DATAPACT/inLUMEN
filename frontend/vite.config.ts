import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";

const frontendPort = Number(process.env.FRONTEND_PORT || 8080);

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    host: "0.0.0.0",
    port: frontendPort,
    strictPort: true,
    watch: {
      usePolling: true,
      interval: 1000,
    },
  },
  plugins: [
    react()
  ].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
}));
