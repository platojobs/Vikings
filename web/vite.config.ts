import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Vite 配置：使用 @ 别名指向 src，开发端口固定 5173
// Element Plus 按需引入：只打包用到的组件，显著减小首屏体积
export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
  build: {
    rollupOptions: {
      output: {
        /**
         * 只拆真正独立的大依赖，避免单个 chunk 超过 500 kB：
         * - echarts / zrender：仅图表页用到，独立缓存
         * - mqtt：应用外壳（MQTT 状态灯）与实时页共用，独立后入口包不再背着它
         * Vue 与 Element Plus 保持默认合并策略：前者入口必用，后者由 unplugin
         * 按需引入自然分包（组件只进各自异步 chunk），整包合并反而会增大首屏加载。
         */
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return
          }
          if (id.includes('echarts') || id.includes('zrender')) {
            return 'echarts'
          }
          if (id.includes('mqtt')) {
            return 'mqtt'
          }
        },
      },
    },
  },
})
