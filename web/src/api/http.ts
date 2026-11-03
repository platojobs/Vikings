/**
 * 统一 HTTP 封装（不引入 axios，基于原生 fetch）。
 *
 * - Base URL 来自环境变量 VITE_API_BASE，默认 http://127.0.0.1:8000
 * - 自动携带 Authorization: Bearer <token>
 * - 按契约解析 { code, message, data }，code !== 0 统一 ElMessage 报错并抛异常
 */

import { ElMessage } from 'element-plus'
import { clearAuth, getToken } from '@/utils/auth'
import type { ApiEnvelope } from '@/types'

const API_BASE = (import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000').replace(/\/+$/, '')

type QueryValue = string | number | boolean | null | undefined

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  params?: Record<string, QueryValue>
  body?: unknown
  signal?: AbortSignal
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
function buildUrl(path: string, params?: Record<string, QueryValue>): string {
  const url = new URL(`${API_BASE}${path.startsWith('/') ? path : `/${path}`}`)
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

/**
 * 发起请求并返回包体中的 data。
 * 失败时已弹出中文提示，调用方通常只需 catch 后忽略即可。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.params), {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    const message = '网络请求失败，请确认后端网关已启动'
    ElMessage.error(message)
    throw new ApiError(message)
  }

  let payload: ApiEnvelope<T> & ErrorBody
  try {
    payload = (await response.json()) as ApiEnvelope<T> & ErrorBody
  } catch {
    const message = `接口返回格式异常（HTTP ${response.status}）`
    ElMessage.error(message)
    throw new ApiError(message, response.status)
  }

  if (!response.ok) {
    const message =
      payload.message || payload.detail || `请求失败（HTTP ${response.status}）`
    if (response.status === 401) clearAuth()
    ElMessage.error(message)
    throw new ApiError(message, payload.code ?? response.status)
  }

  if (payload.code !== 0) {
    const message = payload.message || '业务处理失败'
    ElMessage.error(message)
    throw new ApiError(message, payload.code)
  }

  return payload.data
}
