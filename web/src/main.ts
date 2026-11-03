import { createApp } from 'vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './styles/index.css'

// Element Plus 由 unplugin-vue-components 按需自动注册，
// 这里不再 app.use(ElementPlus)，国际化交给 App.vue 的 el-config-provider。
const app = createApp(App)
app.use(router)
app.mount('#app')
