import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In the container the API and these assets come from one origin, so the
// client asks for '/api/...' and knows nothing about where the server is.
// The dev server keeps that true by proxying '/api' to a locally-run
// `python viewer.py`, which is what makes hot reload possible without
// rebuilding the image or running Docker at all.
//
// 8083, not the 8082 docker-compose publishes: the compose stack is normally
// up, and developing must not mean stopping it. See DEV_PORT in dwb/viewer.py.
const api = process.env.DWB_VIEWER_API ?? 'http://127.0.0.1:8083'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: api } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
