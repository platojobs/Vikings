/** 登录态本地存储：token 与用户信息（无 Pinia，保持轻量） */

const TOKEN_KEY = 'iot_console_token'
// refresh 独立 key：access 每次请求随 Bearer 发送，refresh 只发给 /api/auth/refresh|logout，
// 分开存储便于排查与一次性清理，二者都在 localStorage（同源脚本可读，XSS 风险一致）
const REFRESH_KEY = 'iot_console_refresh'
const USER_KEY = 'iot_console_user'

export interface StoredUser {
  username: string
  role: string
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function getStoredUser(): StoredUser | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as StoredUser
  } catch {
    return null
  }
}

export function getRefreshToken(): string {
  return localStorage.getItem(REFRESH_KEY) ?? ''
}

/** 写入登录态；`refreshToken` 省略时保留已有值（例如仅刷新角色信息） */
export function setAuth(token: string, user: StoredUser, refreshToken?: string): void {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  if (refreshToken) localStorage.setItem(REFRESH_KEY, refreshToken)
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
  localStorage.removeItem(USER_KEY)
}
