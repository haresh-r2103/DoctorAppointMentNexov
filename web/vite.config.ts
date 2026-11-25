import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev, the dashboard runs on :5173 and proxies every /api call (REST, SSE
// and the WebRTC offer) to the Python server on :7860, so the browser only
// ever talks to one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': { target: 'http://localhost:7860', changeOrigin: true } },
  },
})
