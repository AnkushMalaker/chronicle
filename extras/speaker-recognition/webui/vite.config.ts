import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import basicSsl from '@vitejs/plugin-basic-ssl'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), basicSsl()],
  server: {
    host: process.env.REACT_UI_HOST || '0.0.0.0',
    port: parseInt(process.env.REACT_UI_PORT || '5174'),
    https: process.env.REACT_UI_HTTPS === 'true' ? true : false,
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
