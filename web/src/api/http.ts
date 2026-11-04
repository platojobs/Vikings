/**
 * 统一 HTTP 封装（不引入 axios，基于原生 fetch）。
 *
 * - Base URL 来自环境变量 VITE_API_BASE：
 *   开发默认 http://127.0.0.1:8000；生产构建（PROD）默认同源，由反代转发 /api
 * - 自动携带 Authorization: Bearer <token>
 * - 401 时用 refresh 单飞续期（并发请求只触发一次刷新），成功后重放原请求；
 *   refresh 失败则清会话并通知应用回登录页（v1.24.0）
 * - 按契约解析 { code, message, data }，code !== 0 统一 ElMessage 报错并抛异常
 */

import { ElMessage } from 'element-plus'
import { clearAuth, getRefreshToken, getStoredUser, getToken, setAuth } from '@/utils/auth'
import type { ApiEnvelope, LoginResult } from '@/types'

const API_BASE = (
  import.meta.env.VITE_API_BASE ||
  (import.meta.env.PROD ? '' : 'http://127.0.0.1:8000')
).replace(/\/+$/, '')

type QueryValue = string | number | boolean | null | undefined

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  params?: Record<string, QueryValue>
  body?: unknown
  signal?: AbortSignal
  /** 静默模式：不弹 ElMessage（供 MQTT 凭证等后台轮询使用） */
  silent?: boolean
}

/** 业务异常：带上后端 code，便于调用方按错误码做特殊处理 */
export class ApiError extends Error {
  code: number

  constructor(message: string, code = -1) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

/** 拼接 URL 并追加 query，自动过滤空值 */
export function buildApiUrl(path: string, params?: Record<string, QueryValue>): string {
  // 生产构建 VITE_API_BASE 为空（同源），此时是相对路径：new URL 需要基准，
  // 用当前页面 origin 兜底，否则浏览器直接抛 Invalid URL，所有请求都会失败。
  const url = new URL(
    `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`,
    window.location.origin,
  )
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

interface ErrorBody {
  code?: number
  message?: string
  detail?: string
}

/** 登录/刷新/登出本身不触发自动续期，避免 401 递归 */
function isAuthEndpoint(path: string): boolean {
  return (
    path.startsWith('/api/auth/login') ||
    path.startsWith('/api/auth/refresh') ||
    path.startsWith('/api/auth/logout')
  )
}

/** 登录态彻底失效时的通知（由 main.ts 注册为跳转登录页） */
let sessionExpiredHandler: (() => void) | null = null

export function setSessionExpiredHandler(handler: (() => void) | null): void {
  sessionExpiredHandler = handler
}

/**
 * 单飞刷新：模块级 Promise 保证并发 401 只发一次 /api/auth/refresh。
 * - 刷新成功：覆盖 access/refresh，原请求重放一次；
 * - 刷新失败：清空本地会话，交给 sessionExpiredHandler 回登录页。
 */
let refreshPromise: Promise<boolean> | null = null

/** 测试与可观测用途：当前是否已有刷新在途 */
export function isRefreshInFlight(): boolean {
  return refreshPromise !== null
}

export async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise

  const refreshToken = getRefreshToken()
  if (!refreshToken) return false

  refreshPromise = (async () => {
    try {
      const response = await fetch(buildApiUrl('/api/auth/refresh'), {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      const payload = (await response.json()) as ApiEnvelope<LoginResult> & ErrorBody
      if (!response.ok || payload.code !== 0 || !payload.data?.token) return false
      const user = payload.data.user ?? getStoredUser()
      if (user) setAuth(payload.data.token, user, payload.data.refresh_token)
      return true
    } catch {
      return false
    }
  })().finally(() => {
    refreshPromise = null
  })

  return refreshPromise
}

/**
 * 发起请求并返回包体中的 data。
 * 失败时已弹出中文提示，调用方通常只需 catch 后忽略即可。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const allowRefresh = !isAuthEndpoint(path)
  const method = options.method ?? 'GET'

  let body: string | undefined
  let contentType: string | undefined
  if (options.body !== undefined) {
    contentType = 'application/json'
    body = JSON.stringify(options.body)
  }

  for (let attempt = 0; ; attempt += 1) {
    const headers: Record<string, string> = { Accept: 'application/json' }
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
    if (contentType) headers['Content-Type'] = contentType

    let response: Response
    try {
      response = await fetch(buildApiUrl(path, options.params), {
        method,
        headers,
        body,
        signal: options.signal,
      })
    } catch (error) {
      if ((error as Error).name === 'AbortError') throw error
      const message = '网络请求失败，请确认后端网关已启动'
      if (!options.silent) ElMessage.error(message)
      throw new ApiError(message)
    }

    let payload: ApiEnvelope<T> & ErrorBody
    try {
      payload = (await response.json()) as ApiEnvelope<T> & ErrorBody
    } catch {
      const message = `接口返回格式异常（HTTP ${response.status}）`
      if (!options.silent) ElMessage.error(message)
      throw new ApiError(message, response.status)
    }

    if (!response.ok) {
      if (response.status === 401 && allowRefresh) {
        if (attempt === 0) {
          // 并发请求里只要有一个先完成刷新，其余请求直接换新 token 重放，
          // 不再各自发起 refresh（token 已变说明别人刷新成功）
          const latest = getToken()
          if (latest && latest !== token) continue
          if (await refreshAccessToken()) continue // 刷新失败才落到下面
        }
        clearAuth()
        sessionExpiredHandler?.()
        const message = '登录已过期，请重新登录'
        if (!options.silent) ElMessage.error(message)
        throw new ApiError(message, 401)
      }

      const message =
        payload.message || payload.detail || `请求失败（HTTP ${response.status}）`
      if (response.status === 401) clearAuth()
      if (!options.silent) ElMessage.error(message)
      throw new ApiError(message, payload.code ?? response.status)
    }

    if (payload.code !== 0) {
      const message = payload.message || '业务处理失败'
      if (!options.silent) ElMessage.error(message)
      throw new ApiError(message, payload.code)
    }

    return payload.data
  }
}
