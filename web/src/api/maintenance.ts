/** 维护窗口接口（v1.39.0）：admin 写，登录用户只读 */
import { request } from '@/api/http'
import type {
  MaintenanceSummary,
  MaintenanceSuppression,
  MaintenanceWindow,
  MaintenanceWindowCreateBody,
  PageResult,
} from '@/types'

export function fetchMaintenanceWindows(): Promise<MaintenanceWindow[]> {
  return request<MaintenanceWindow[]>('/api/maintenance/windows')
}

export function createMaintenanceWindow(
  body: MaintenanceWindowCreateBody,
): Promise<MaintenanceWindow> {
  return request<MaintenanceWindow>('/api/maintenance/windows', { method: 'POST', body })
}

export function endMaintenanceWindow(windowId: string): Promise<MaintenanceWindow> {
  return request<MaintenanceWindow>(`/api/maintenance/windows/${windowId}/end`, {
    method: 'POST',
  })
}

export function fetchMaintenanceSummaries(windowId: string): Promise<MaintenanceSummary[]> {
  return request<MaintenanceSummary[]>(`/api/maintenance/windows/${windowId}/summaries`)
}

export function fetchMaintenanceSuppressions(
  windowId: string,
  page = 1,
  pageSize = 20,
): Promise<PageResult<MaintenanceSuppression>> {
  return request<PageResult<MaintenanceSuppression>>(
    `/api/maintenance/windows/${windowId}/suppressions`,
    { params: { page, pageSize } },
  )
}
