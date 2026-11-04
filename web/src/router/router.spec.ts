import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, type Router } from 'vue-router'
import { createAppRouter } from '@/router'
import { clearAuth, setAuth } from '@/utils/auth'
import { setRole } from '@/utils/permissions'

/** 每个用例独立 router + memory history，避免导航记录与登录态串味 */
function makeRouter(): Router {
  return createAppRouter(createMemoryHistory())
}

function loginAs(role: 'admin' | 'operator' | 'viewer'): void {
  setAuth('test-token', { username: role, role })
  setRole(role)
}

async function go(router: Router, path: string) {
  await router.push(path)
  return router.currentRoute.value
}

beforeEach(() => {
  clearAuth()
  setRole('')
})

describe('路由守卫：登录态', () => {
  it('未登录访问受保护路由 → /login 并携带 redirect', async () => {
    const router = makeRouter()
    const route = await go(router, '/devices?keyword=tower')

    expect(route.path).toBe('/login')
    expect(route.name).toBe('login')
    expect(route.query.redirect).toBe('/devices?keyword=tower')
  })

  it('未登录访问根路径 → 先落到 /login，redirect 记录 /dashboard', async () => {
    const router = makeRouter()
    const route = await go(router, '/')

    expect(route.path).toBe('/login')
    expect(route.query.redirect).toBe('/dashboard')
  })

  it('已登录访问 /login → 回到 /dashboard', async () => {
    loginAs('admin')
    const router = makeRouter()
    const route = await go(router, '/login')

    expect(route.path).toBe('/dashboard')
    expect(route.name).toBe('dashboard')
  })
})

describe('路由守卫：管理员专属页面', () => {
  it('admin 访问 /system → 允许进入系统状态页', async () => {
    loginAs('admin')
    const router = makeRouter()
    const route = await go(router, '/system')

    expect(route.path).toBe('/system')
    expect(route.name).toBe('system')
    expect(route.redirectedFrom).toBeUndefined()
  })

  it('viewer 访问 /system → 被拒并回到 /dashboard', async () => {
    loginAs('viewer')
    const router = makeRouter()
    const route = await go(router, '/system')

    expect(route.path).toBe('/dashboard')
    expect(route.name).toBe('dashboard')
  })

  it('operator 访问 /users → 被拒并回到 /dashboard', async () => {
    loginAs('operator')
    const router = makeRouter()
    const route = await go(router, '/users')

    expect(route.path).toBe('/dashboard')
    expect(route.name).toBe('dashboard')
  })
})

describe('路由兜底', () => {
  it('已登录访问未知路径 → 落到兜底路由 /dashboard', async () => {
    loginAs('admin')
    const router = makeRouter()
    const route = await go(router, '/no-such-page/deep')

    expect(route.path).toBe('/dashboard')
    expect(route.name).toBe('dashboard')
  })
})
