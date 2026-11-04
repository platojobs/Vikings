/** 认证接口：登录与登录后的账号状态刷新 */

import { request } from '@/api/http'
import type { AppUser, LoginBody, LoginResult } from '@/types'

export function login(body: LoginBody): Promise<LoginResult> {
  return request<LoginResult>('/api/auth/login', { method: 'POST', body })
}

/** 拉取数据库中的最新账号状态（角色可能被管理员调整） */
export function fetchMe(): Promise<AppUser> {
  return request<AppUser>('/api/auth/me')
}

/** 登出：服务端吊销当前 refresh（幂等），随后客户端清本地会话 */
export function logout(refreshToken: string): Promise<{ revoked: boolean }> {
  return request<{ revoked: boolean }>('/api/auth/logout', {
    method: 'POST',
    body: { refresh_token: refreshToken },
  })
}
