import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// The founder design (Figma Make export) as the FLOPX frontend. The Figma preview plugins of the
// export are dropped; the app, its components and styles are unchanged.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    port: parseInt(process.env.PORT || '5300'),
    strictPort: true,
    // Dev only: the backend's CORS allowlist admits *.vercel.app and localhost:3000, not every
    // Vite port, so the dev server proxies /flopx/* to it and src/live/client.ts uses a relative
    // base in dev. (/__flopx is kept as an alias for any server started before the switch.)
    proxy: {
      '/flopx': { target: process.env.VITE_API_BASE_URL || 'https://flopx-backend.onrender.com', changeOrigin: true },
      // Sign-in (src/live/auth.ts) posts to /auth/* through the same proxy in dev.
      '/auth': { target: process.env.VITE_API_BASE_URL || 'https://flopx-backend.onrender.com', changeOrigin: true },
      '/__flopx': { target: process.env.VITE_API_BASE_URL || 'https://flopx-backend.onrender.com', changeOrigin: true, rewrite: (p) => p.replace(/^\/__flopx/, '') },
    },
  },
})
