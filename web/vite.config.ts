import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：使用 @ 别名指向 src，开发端口固定 5173
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
})
