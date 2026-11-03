/** OTA 任务接口 */

import { request } from '@/api/http'
import type { OtaTask, OtaTaskCreateBody, PageResult } from '@/types'

/** 创建任务：入参必须是 camelCase（deviceIds / targetVersion），对应网关字段别名 */
export function createOtaTask(body: OtaTaskCreateBody): Promise<OtaTask> {
  return request<OtaTask>('/api/ota/tasks', { method: 'POST', body })
}

export function fetchOtaTasks(page = 1, pageSize = 10): Promise<PageResult<OtaTask>> {
  return request<PageResult<OtaTask>>('/api/ota/tasks', { params: { page, pageSize } })
}

export function fetchOtaTaskDetail(taskId: string): Promise<OtaTask> {
  return request<OtaTask>(`/api/ota/tasks/${encodeURIComponent(taskId)}`)
}
