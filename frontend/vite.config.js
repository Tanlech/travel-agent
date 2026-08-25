import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = 'http://localhost:8001'

export default defineConfig({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/chat': backend,
      '/auth': backend,
      '/session': backend,
      '/sessions': backend,
      '/memory': backend,
      '/admin': backend,
      '/health': backend,
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: false,
  },
})