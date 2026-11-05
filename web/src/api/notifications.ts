/** 告警通知通道配置与投递记录（v1.29.0，仅管理员配置，operator 可发测试） */

import { request } from '@/api/http'
import type {
  NotificationConfig,
  NotificationConfigPatch,
  NotificationDelivery,
  NotificationDeliveryStatus,
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

/** 真实发送测试通知：忽略规则与静默，逐通道返回结果 */
export function sendTestNotification(
  channel: NotificationTestChannel = 'all',
): Promise<NotificationTestResult[]> {
  return request<NotificationTestResult[]>('/api/system/notifications/test', {
    method: 'POST',
    body: { channel },
  })
}

export function fetchNotificationDeliveries(
  status?: NotificationDeliveryStatus,
  pageSize = 20,
): Promise<NotificationDelivery[]> {
  return request<NotificationDelivery[]>('/api/system/notifications/deliveries', {
    params: { status, page: 1, page_size: pageSize },
  })
}
