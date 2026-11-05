import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 告警通知 API（v1.29.0）：
 * - 配置读写走 /api/system/notifications/config；
 * - 测试通知必须 POST 真实通道，不允许前端本地伪造结果；
 * - 投递记录按状态过滤，空状态不发送冗余 query。
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import {
  fetchNotificationConfig,
  fetchNotificationDeliveries,
  sendTestNotification,
  updateNotificationConfig,
} from '@/api/notifications'
import type { NotificationConfig } from '@/types'

const config: NotificationConfig = {
  enabled: true,
  webhook_enabled: true,
  webhook_url_configured: true,
  webhook_secret_configured: true,
  webhook_timeout_seconds: 10,
  webhook_max_attempts: 3,
  smtp_enabled: true,
  smtp_host: 'smtp.example.com',
  smtp_port: 587,
  smtp_username: 'alerts@example.com',
  smtp_password_configured: true,
  smtp_from: 'alerts@example.com',
  smtp_to: 'oncall@example.com',
  smtp_starttls: true,
  smtp_timeout_seconds: 10,
  smtp_max_attempts: 3,
  levels: ['critical'],
  groups: [],
  device_allow: [],
  device_deny: [],
  mute_minutes: 10,
  retry_backoff_seconds: 1,
  updated_at: '2026-11-05T03:00:00Z',
  updated_by: 'ops',
}

beforeEach(() => {
  requestMock.mockReset()
})

describe('通知配置 API', () => {
  it('读取配置不回显敏感值', async () => {
    requestMock.mockResolvedValue(config)
    const data = await fetchNotificationConfig()

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/config')
    expect(data.webhook_url_configured).toBe(true)
    expect(data).not.toHaveProperty('webhook_url')
    expect(data).not.toHaveProperty('webhook_secret')
    expect(data).not.toHaveProperty('smtp_password')
  })

  it('更新配置使用 PATCH 且只提交调用方给出的字段', async () => {
    requestMock.mockResolvedValue(config)
    await updateNotificationConfig({ mute_minutes: 30, levels: ['warning', 'critical'] })

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/config', {
      method: 'PATCH',
      body: { mute_minutes: 30, levels: ['warning', 'critical'] },
    })
  })
})

describe('测试通知与投递记录', () => {
  it('测试通知按通道 POST，等待真实结果', async () => {
    requestMock.mockResolvedValue([
      { channel: 'webhook', status: 'sent', attempts: 1, error: null },
      { channel: 'smtp', status: 'failed', attempts: 3, error: 'SMTP SMTPConnectError' },
    ])
    const results = await sendTestNotification('all')

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/test', {
      method: 'POST',
      body: { channel: 'all' },
    })
    expect(results).toHaveLength(2)
    expect(results[1].status).toBe('failed')
  })

  it('投递记录可按状态过滤', async () => {
    requestMock.mockResolvedValue([])
    await fetchNotificationDeliveries('failed')

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/deliveries', {
      params: { status: 'failed', page: 1, page_size: 20 },
    })
  })

  it('未选状态时不携带 status', async () => {
    requestMock.mockResolvedValue([])
    await fetchNotificationDeliveries()

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/deliveries', {
      params: { status: undefined, page: 1, page_size: 20 },
    })
  })
})
