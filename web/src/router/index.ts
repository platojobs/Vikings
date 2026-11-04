import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { getToken } from '@/utils/auth'
import { currentRole } from '@/utils/permissions'

// 所有路由（含登录页）统一按需加载，入口包只保留应用外壳与守卫
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  { path: '/', redirect: '/dashboard' },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: '实时仪表盘' },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('@/views/DevicesView.vue'),
    meta: { title: '设备管理' },
  },
  {
    path: '/history',
    name: 'history',
    component: () => import('@/views/HistoryView.vue'),
    meta: { title: '历史查询' },
  },
  {
    path: '/alerts',
    name: 'alerts',
    component: () => import('@/views/AlertsView.vue'),
    meta: { title: '告警中心' },
  },
  {
    path: '/audit',
    name: 'audit',
    component: () => import('@/views/AuditView.vue'),
    meta: { title: '审计日志' },
  },
  {
    path: '/users',
    name: 'users',
    component: () => import('@/views/UsersView.vue'),
    meta: { title: '账号管理' },
  },
  {
    path: '/ota',
    name: 'ota',
    component: () => import('@/views/OtaView.vue'),
    meta: { title: 'OTA 管理' },
  },
  {
    path: '/system',
    name: 'system',
    component: () => import('@/views/SystemView.vue'),
    meta: { title: '系统状态' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/** 未登录统一重定向到登录页，并记录回跳地址 */
router.beforeEach((to) => {
  const token = getToken()
  if (!to.meta.public && !token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && token) {
    return { path: '/dashboard' }
  }
  // 账号管理与系统状态仅对管理员开放，非管理员直接回到仪表盘
  if ((to.path === '/users' || to.path === '/system') && currentRole() !== 'admin') {
    return { path: '/dashboard' }
  }
  return true
})

export default router
