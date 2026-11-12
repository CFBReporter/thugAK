import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The engine runs as a separate FastAPI process. Proxying in dev means the
    // front-end uses the same relative /api paths it will use in production,
    // so no build-time base URL and no CORS surprises on deploy.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
