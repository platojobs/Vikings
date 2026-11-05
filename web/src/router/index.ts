import {
  createRouter,
  createWebHistory,
  type RouteLocationNormalized,
  type RouteLocationRaw,
  type RouteRecordRaw,
  type RouterHistory,
} from 'vue-router'
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
  {
    // v1.31.0：通知投递历史独立页面（operator 值班排障 + admin）
    path: '/notifications',
    name: 'notification-deliveries',
    component: () => import('@/views/NotificationDeliveriesView.vue'),
    meta: { title: '通知投递' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

/** 未登录统一重定向到登录页，并记录回跳地址 */
export function authGuard(to: RouteLocationNormalized): true | RouteLocationRaw {
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
  // 投递历史含接收端错误细节：viewer 不可见，operator/admin 可看
  if (to.path === '/notifications' && currentRole() === 'viewer') {
    return { path: '/dashboard' }
  }
  return true
}

/** 构造 router：生产用浏览器 history，单元测试注入 memory history 隔离状态 */
export function createAppRouter(history: RouterHistory = createWebHistory()) {
  const router = createRouter({ history, routes })
  router.beforeEach(authGuard)
  return router
}

const router = createAppRouter()

export default router
