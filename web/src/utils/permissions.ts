/**
 * 前端权限判定：与后端 require_role 一一对应。
 * 角色来自登录响应，并由 App.vue 在启动时通过 /api/auth/me 刷新，
 * 因此这里用 ref 保存角色，模板中的 computed 可自动跟随变化。
 */

import { ref } from 'vue'
import { getStoredUser } from '@/utils/auth'
import type { UserRole } from '@/types'

const role = ref<UserRole | ''>((getStoredUser()?.role as UserRole) ?? '')

/** 登录成功或角色刷新后由应用外壳写入 */
export function setRole(next: UserRole | ''): void {
  role.value = next
}

export function currentRole(): UserRole | '' {
  return role.value
}

/** 管理员：用户管理 + 阈值配置 */
export function isAdmin(): boolean {
  return role.value === 'admin'
}

/** 可执行运维动作（下发指令 / OTA / 确认告警）：admin + operator */
export function canOperate(): boolean {
  return role.value === 'admin' || role.value === 'operator'
}
