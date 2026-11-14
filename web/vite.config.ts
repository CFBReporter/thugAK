import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bound explicitly to IPv4 loopback. Left unset, Vite picked the IPv6
    // loopback only, so http://127.0.0.1:5173 was refused while
    // http://[::1]:5173 worked -- a browser that resolves "localhost" to
    // 127.0.0.1 could not reach the app at all. This also matches the API
    // process, which uvicorn binds at 127.0.0.1, so both halves of the stack
    // live on the same address family. Still loopback-only: nothing on the
    // network can reach it.
    host: '127.0.0.1',
    strictPort: true,
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
