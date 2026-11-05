/** 告警接口：列表、单条确认、批量确认、统计 */

import { request } from '@/api/http'
import type {
  AlertItem,
  AlertListQuery,
  AlertSummary,
  BulkAckResult,
  PageResult,
} from '@/types'

export function fetchAlerts(query: AlertListQuery = {}): Promise<PageResult<AlertItem>> {
  return request<PageResult<AlertItem>>('/api/alerts', {
    params: {
      deviceId: query.deviceId,
      level: query.level,
      status: query.status,
      start: query.start,
      end: query.end,
      page: query.page,
      pageSize: query.pageSize,
    },
  })
}

/** 单条确认（v1.21.0）：备注可选；不传备注时不发送 body，兼容旧调用方 */
export function ackAlert(alertId: string, note?: string): Promise<AlertItem> {
  const text = note?.trim()
  return request<AlertItem>(`/api/alerts/${encodeURIComponent(alertId)}/ack`, {
    method: 'POST',
    body: text ? { note: text } : undefined,
  })
}

/**
 * 批量确认（复用既有 POST /api/alerts/bulk-ack，不新增第二套端点）。
 * 结果按 acked / already_acked / not_found 返回，调用方必须逐类展示。
 */
export function bulkAckAlerts(alertIds: string[], note?: string): Promise<BulkAckResult> {
  const text = note?.trim()
  return request<BulkAckResult>('/api/alerts/bulk-ack', {
    method: 'POST',
    body: text ? { alert_ids: alertIds, note: text } : { alert_ids: alertIds },
  })
}

export function fetchAlertSummary(): Promise<AlertSummary> {
  return request<AlertSummary>('/api/alerts/summary')
}
