/** 账号管理接口（仅 admin 可调用） */

import { request } from '@/api/http'
import type { AppUser, UserCreateBody, UserUpdateBody } from '@/types'

export function fetchUsers(): Promise<AppUser[]> {
  return request<AppUser[]>('/api/users')
}

export function createUser(body: UserCreateBody): Promise<AppUser> {
  return request<AppUser>('/api/users', { method: 'POST', body })
}

export function updateUser(username: string, body: UserUpdateBody): Promise<AppUser> {
  return request<AppUser>(`/api/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    body,
  })
}

export function deleteUser(username: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(`/api/users/${encodeURIComponent(username)}`, {
    method: 'DELETE',
  })
}
