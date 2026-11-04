/** 系统状态接口（仅管理员） */

import { request } from '@/api/http'
import type { SystemStatus } from '@/types'

/** 网关 / Broker / 存储 / 后台任务的只读快照 */
export function fetchSystemStatus(): Promise<SystemStatus> {
  return request<SystemStatus>('/api/system/status')
}
