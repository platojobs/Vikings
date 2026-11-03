import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './styles/index.css'

const app = createApp(App)

// 全量注册 Element Plus，并指定中文语言包（日期选择器等组件需要）
app.use(ElementPlus, { locale: zhCn })
app.use(router)
app.mount('#app')
