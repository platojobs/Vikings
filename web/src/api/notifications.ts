/** 告警通知：通道传输配置、多规则、测试通知与投递历史（v1.31.0）。 */

import { request } from '@/api/http'
import type {
  NotificationConfig,
  NotificationConfigPatch,
  NotificationDeliveryChannel,
  NotificationDeliveryPage,
  NotificationDeliveryStatus,
  NotificationRule,
  NotificationRulePatch,
  NotificationRulePayload,
  NotificationTestChannel,
  NotificationTestResult,
} from '@/types'

export function fetchNotificationConfig(): Promise<NotificationConfig> {
  return request<NotificationConfig>('/api/system/notifications/config')
}

export function updateNotificationConfig(
  patch: NotificationConfigPatch,
): Promise<NotificationConfig> {
  return request<NotificationConfig>('/api/system/notifications/config', {
    method: 'PATCH',
    body: patch,
  })
}

/** 规则列表按优先级（priority ASC, id ASC）返回，顺序即裁决顺序 */
export function fetchNotificationRules(): Promise<NotificationRule[]> {
  return request<NotificationRule[]>('/api/system/notifications/rules')
}

export function createNotificationRule(payload: NotificationRulePayload): Promise<NotificationRule> {
  return request<NotificationRule>('/api/system/notifications/rules', {
    method: 'POST',
    body: payload,
  })
}

export function updateNotificationRule(
  ruleId: number,
  patch: NotificationRulePatch,
): Promise<NotificationRule> {
  return request<NotificationRule>(`/api/system/notifications/rules/${ruleId}`, {
    method: 'PATCH',
    body: patch,
  })
}

export function deleteNotificationRule(ruleId: number): Promise<{ deleted: number }> {
  return request<{ deleted: number }>(`/api/system/notifications/rules/${ruleId}`, {
    method: 'DELETE',
  })
}

/** 真实发送测试通知：忽略规则匹配与静默；可指定规则验证其接收人 */
export function sendTestNotification(
  channel: NotificationTestChannel = 'all',
  ruleId?: number,
): Promise<NotificationTestResult[]> {
  return request<NotificationTestResult[]>('/api/system/notifications/test', {
    method: 'POST',
    body: { channel, rule_id: ruleId },
  })
}

export interface NotificationDeliveryFilters {
  page?: number
  page_size?: number
  status?: NotificationDeliveryStatus | ''
  channel?: NotificationDeliveryChannel | ''
  device_id?: string
  start?: string
  end?: string
}

/** 投递历史：筛选与分页语义全部由服务端决定，前端只透传 */
export function fetchNotificationDeliveries(
  filters: NotificationDeliveryFilters = {},
): Promise<NotificationDeliveryPage> {
  return request<NotificationDeliveryPage>('/api/system/notifications/deliveries', {
    params: {
      page: filters.page ?? 1,
      page_size: filters.page_size ?? 20,
      status: filters.status || undefined,
      channel: filters.channel || undefined,
      device_id: filters.device_id || undefined,
      start: filters.start || undefined,
      end: filters.end || undefined,
    },
  })
}
