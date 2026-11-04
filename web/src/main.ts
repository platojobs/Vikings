import { createApp } from 'vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { setSessionExpiredHandler } from './api/http'
import './styles/index.css'

// refresh 失败（会话被登出/改密/管理员吊销/过期）→ 清会话已完成，这里负责回登录页
setSessionExpiredHandler(() => {
  if (router.currentRoute.value.path === '/login') return
  void router.replace({
    path: '/login',
    query: { redirect: router.currentRoute.value.fullPath },
  })
})

// Element Plus 由 unplugin-vue-components 按需自动注册，
// 这里不再 app.use(ElementPlus)，国际化交给 App.vue 的 el-config-provider。
const app = createApp(App)
app.use(router)
app.mount('#app')
