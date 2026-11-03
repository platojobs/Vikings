/** 认证接口 */

import { request } from '@/api/http'
import type { LoginBody, LoginResult } from '@/types'

export function login(body: LoginBody): Promise<LoginResult> {
  return request<LoginResult>('/api/auth/login', { method: 'POST', body })
}
