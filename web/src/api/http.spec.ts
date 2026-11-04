/**
 * v1.24.0 / P2-4：Web 侧 401 自动续期与单飞回归。
 *
 * 用 mock fetch 复现「5 个请求同时 401」的场景，断言只发生 1 次 /api/auth/refresh；
 * 以及 refresh 失败时会清空本地 access/refresh 并通知应用回登录页。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

type MockResponse = Response

function reply(body: unknown, status = 200): MockResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as MockResponse
}

function envelope(data: unknown, code = 0) {
  return { code, message: code === 0 ? 'ok' : 'error', data }
}

describe('HTTP 401 自动续期', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
    vi.unstubAllEnvs()
  })

  it('生产同源构建（VITE_API_BASE 为空）时相对路径解析到当前 origin', async () => {
    // 真实生产镜像用 VITE_API_BASE="" 构建；回归 new URL 缺基准导致的 Invalid URL
    vi.stubEnv('VITE_API_BASE', '/')
    vi.resetModules()
    const { buildApiUrl } = await import('@/api/http')
    expect(buildApiUrl('/api/health')).toBe(`${window.location.origin}/api/health`)
    expect(buildApiUrl('/api/devices', { page: 1, keyword: 'a b' })).toContain(
      `/api/devices?page=1&keyword=a+b`,
    )
  })

  it('并发 5 个 401 只触发 1 次 refresh 并重放原请求', async () => {
    const { setAuth, getToken, getRefreshToken } = await import('@/utils/auth')
    const { request } = await import('@/api/http')

    setAuth('expired-access', { username: 'admin', role: 'admin' }, 'refresh-1')

    let refreshCalls = 0
    let deviceCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input)).pathname
        const auth = new Headers(init?.headers).get('Authorization')
        if (path === '/api/auth/refresh') {
          refreshCalls += 1
          return reply(
            envelope({
              token: 'new-access',
              refresh_token: 'refresh-2',
              user: { username: 'admin', role: 'admin' },
            }),
          )
        }
        deviceCalls += 1
        if (auth === 'Bearer expired-access') {
          return reply({ detail: '令牌已过期' }, 401)
        }
        if (auth === 'Bearer new-access') {
          return reply(envelope({ items: [], total: 0 }))
        }
        return reply({ detail: '无法识别令牌' }, 403)
      }),
    )

    const results = await Promise.all(
      Array.from({ length: 5 }, () => request<unknown>('/api/devices', { silent: true })),
    )

    expect(results).toHaveLength(5)
    expect(refreshCalls).toBe(1)
    expect(deviceCalls).toBe(10) // 5 次 401 + 5 次重放
    expect(getToken()).toBe('new-access')
    expect(getRefreshToken()).toBe('refresh-2')
  })

  it('refresh 失败：清空会话并触发回登录页回调', async () => {
    const { setAuth, getToken, getRefreshToken } = await import('@/utils/auth')
    const { request, setSessionExpiredHandler, ApiError } = await import('@/api/http')

    setAuth('expired-access', { username: 'admin', role: 'admin' }, 'revoked-refresh')
    const expired = vi.fn()
    setSessionExpiredHandler(expired)

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input)).pathname
        if (path === '/api/auth/refresh') {
          return reply({ detail: '刷新令牌已被吊销' }, 401)
        }
        return reply({ detail: '令牌已过期' }, 401)
      }),
    )

    await expect(request<unknown>('/api/devices', { silent: true })).rejects.toBeInstanceOf(
      ApiError,
    )
    expect(expired).toHaveBeenCalledTimes(1)
    expect(getToken()).toBe('')
    expect(getRefreshToken()).toBe('')
  })
})
