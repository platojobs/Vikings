/** 告警接口：列表、确认、统计、SLA 配置与值班交接（v1.33.0） */

import { request } from '@/api/http'
import type {
  AlertHandoverResult,
  AlertItem,
  AlertListQuery,
  AlertSummary,
  BulkAckResult,
  PageResult,
  SlaConfig,
} from '@/types'

export function fetchAlerts(query: AlertListQuery = {}): Promise<PageResult<AlertItem>> {
  return request<PageResult<AlertItem>>('/api/alerts', {
    params: {
      deviceId: query.deviceId,
      level: query.level,
      status: query.status,
      sla: query.sla,
      // v1.35.0：多个标签按重复参数下发，服务端按「或」匹配
      tag: query.tag,
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

export function fetchSlaConfig(): Promise<SlaConfig> {
  return request<SlaConfig>('/api/alerts/sla-config')
}

/** 仅 admin 可调用；修改只影响新告警，历史告警保留创建时的时限快照 */
export function updateSlaConfig(payload: Partial<SlaConfig>): Promise<SlaConfig> {
  return request<SlaConfig>('/api/alerts/sla-config', { method: 'PUT', body: payload })
}

/** 值班交接：把 from_user（默认当前登录人）名下未处理告警交给下一班 */
export function handoverAlerts(payload: {
  to: string
  from_user?: string
  include_unassigned?: boolean
  note?: string
}): Promise<AlertHandoverResult> {
  return request<AlertHandoverResult>('/api/alerts/handover', { method: 'POST', body: payload })
}
