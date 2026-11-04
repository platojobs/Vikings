import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// 复用生产 Vite 配置（Vue SFC、自动导入、@ 别名），避免测试环境与构建环境两套解析规则
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      // 路由守卫依赖 localStorage，因此需要浏览器 DOM 环境
      environment: 'jsdom',
      include: ['src/**/*.spec.ts'],
      setupFiles: ['./test/setup.ts'],
      // 每个用例自行准备登录态，避免用例之间互相污染
      restoreMocks: true,
      clearMocks: true,
      server: {
        deps: {
          // Element Plus 由 unplugin 按需引入后会带上 CSS，必须交给 Vite 处理；
          // 默认 external 会落到 Node ESM 加载器并因 .css 后缀报错
          inline: ['element-plus'],
        },
      },
    },
  }),
)
