/** 告警规则接口（v1.34.0）：规则 CRUD + v1.33.0 阈值接口兼容层 */
import { request } from '@/api/http'
import type {
  AlertRule,
  AlertRuleCreateBody,
  AlertRuleHistoryEntry,
  AlertRuleUpdateBody,
  ThresholdsUpdateBody,
  ThresholdsUpdateResult,
  ThresholdsView,
} from '@/types'

/** 规则列表（登录可读，operator/viewer 只读） */
export function fetchAlertRules(): Promise<AlertRule[]> {
  return request<AlertRule[]>('/api/rules')
}

/** 新增规则（仅 admin） */
export function createAlertRule(body: AlertRuleCreateBody): Promise<AlertRule> {
  return request<AlertRule>('/api/rules', { method: 'POST', body })
}

/** 修改规则（仅 admin） */
export function updateAlertRule(id: number, body: AlertRuleUpdateBody): Promise<AlertRule> {
  return request<AlertRule>(`/api/rules/${id}`, { method: 'PUT', body })
}

/** 启用/停用规则（仅 admin） */
export function setAlertRuleEnabled(id: number, enabled: boolean): Promise<AlertRule> {
  return request<AlertRule>(`/api/rules/${id}/enabled`, { method: 'PATCH', body: { enabled } })
}

/** 删除规则（仅 admin） */
export function deleteAlertRule(id: number): Promise<{ deleted: number }> {
  return request<{ deleted: number }>(`/api/rules/${id}`, { method: 'DELETE' })
}

/** v1.35.0：规则变更历史，直接复用既有审计日志，不另建记录 */
export function fetchAlertRuleHistory(
  id: number,
  limit = 50,
): Promise<AlertRuleHistoryEntry[]> {
  return request<AlertRuleHistoryEntry[]>(`/api/rules/${id}/history`, {
    params: { limit },
  })
}

/** 把默认规则阈值回滚到出厂/环境变量值（仅 admin） */
export function restoreDefaultAlertRules(): Promise<{ restored: number }> {
  return request<{ restored: number }>('/api/rules/restore-defaults', { method: 'POST' })
}

/** v1.33.0 兼容：查询当前生效阈值 */
export function fetchThresholds(): Promise<ThresholdsView> {
  return request<ThresholdsView>('/api/rules/thresholds')
}

/** v1.33.0 兼容：在线更新阈值（会同步到对应默认规则） */
export function updateThresholds(body: ThresholdsUpdateBody): Promise<ThresholdsUpdateResult> {
  return request<ThresholdsUpdateResult>('/api/rules/thresholds', {
    method: 'PUT',
    body,
  })
}
