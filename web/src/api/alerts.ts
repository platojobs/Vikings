/** 告警接口：列表、确认、统计 */

import { request } from '@/api/http'
import type { AlertItem, AlertListQuery, AlertSummary, PageResult } from '@/types'

export function fetchAlerts(query: AlertListQuery = {}): Promise<PageResult<AlertItem>> {
  return request<PageResult<AlertItem>>('/api/alerts', {
    params: {
      deviceId: query.deviceId,
      level: query.level,
      status: query.status,
      page: query.page,
      pageSize: query.pageSize,
    },
  })
}

export function ackAlert(alertId: string): Promise<AlertItem> {
  return request<AlertItem>(`/api/alerts/${encodeURIComponent(alertId)}/ack`, {
    method: 'POST',
  })
}

export function fetchAlertSummary(): Promise<AlertSummary> {
  return request<AlertSummary>('/api/alerts/summary')
}
