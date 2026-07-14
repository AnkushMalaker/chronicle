import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
// The dev server runs plain HTTP. When HTTPS is enabled, Caddy terminates TLS
// (Tailscale/Let's Encrypt cert) and reverse-proxies to this server over HTTP —
// same as the advanced backend's webui-dev. No self-signed cert in the dev server.
export default defineConfig({
  plugins: [react()],
  server: {
    host: process.env.REACT_UI_HOST || '0.0.0.0',
    port: parseInt(process.env.REACT_UI_PORT || '5174'),
    allowedHosts: process.env.VITE_ALLOWED_HOSTS
      ? process.env.VITE_ALLOWED_HOSTS.split(' ').map(host => host.trim()).filter(host => host.length > 0)
      : [
          'localhost',
          '127.0.0.1',
          '.nip.io'
        ],
  },
  define: {
    global: 'globalThis',
  },
  resolve: {
    alias: {
      buffer: 'buffer',
      stream: 'stream-browserify',
      util: 'util'
    }
  },
  optimizeDeps: {
    include: ['buffer', 'stream-browserify', 'util']
  }
})
