import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/',
  server: {
    port: 5173,
    host: '0.0.0.0',
    allowedHosts: process.env.VITE_ALLOWED_HOSTS
      ? process.env.VITE_ALLOWED_HOSTS.split(' ').map(host => host.trim()).filter(host => host.length > 0)
      : [
          'localhost',
          '127.0.0.1',
          '.nip.io'
        ],
    hmr: {
      port: 5173,
      // Allow HMR to work through proxy
      clientPort: process.env.VITE_HMR_PORT ? parseInt(process.env.VITE_HMR_PORT) : undefined,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
