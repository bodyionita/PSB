import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Web knows exactly one server fact: the API base URL (ADR-006). In dev we proxy /api to
// the FastAPI service so cookies are same-origin; in prod Caddy serves both from one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
