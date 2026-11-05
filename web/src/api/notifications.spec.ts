import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 告警通知 API（v1.31.0）：
 * - 配置/规则读写只走服务端，敏感值永不下发；
 * - 规则按优先级由服务端排序，前端不改顺序；
 * - 投递历史分页与筛选全部透传给服务端（同一套语义）；
 * - 测试通知必须 POST 真实通道，可指定规则。
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import {
  createNotificationRule,
  deleteNotificationRule,
  fetchNotificationConfig,
  fetchNotificationDeliveries,
  fetchNotificationRules,
  sendTestNotification,
  updateNotificationConfig,
  updateNotificationRule,
} from '@/api/notifications'
import type { NotificationConfig, NotificationRule } from '@/types'

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

const rule: NotificationRule = {
  id: 7,
  name: '华东值班',
  enabled: true,
  priority: 10,
  levels: ['critical'],
  groups: ['华东'],
  device_allow: [],
  device_deny: [],
  webhook_enabled: true,
  webhook_url_configured: true,
  webhook_secret_configured: true,
  smtp_enabled: false,
  smtp_to: null,
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

describe('通知规则 API', () => {
  it('列表直接使用服务端优先级顺序', async () => {
    requestMock.mockResolvedValue([rule])
    const data = await fetchNotificationRules()

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/rules')
    expect(data[0].priority).toBe(10)
  })

  it('创建规则提交接收人与分组，不提交未填的敏感字段', async () => {
    requestMock.mockResolvedValue(rule)
    await createNotificationRule({
      name: '华东值班',
      enabled: true,
      priority: 10,
      levels: ['critical'],
      groups: ['华东'],
      device_allow: [],
      device_deny: [],
      webhook_enabled: true,
      smtp_enabled: false,
      smtp_to: '',
    })

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/rules', {
      method: 'POST',
      body: {
        name: '华东值班',
        enabled: true,
        priority: 10,
        levels: ['critical'],
        groups: ['华东'],
        device_allow: [],
        device_deny: [],
        webhook_enabled: true,
        smtp_enabled: false,
        smtp_to: '',
      },
    })
  })

  it('更新规则使用 PATCH，删除使用 DELETE', async () => {
    requestMock.mockResolvedValue(rule)
    await updateNotificationRule(7, { priority: 5 })

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/rules/7', {
      method: 'PATCH',
      body: { priority: 5 },
    })

    requestMock.mockResolvedValue({ deleted: 7 })
    await deleteNotificationRule(7)
    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/rules/7', {
      method: 'DELETE',
    })
  })
})

describe('测试通知与投递历史', () => {
  it('测试通知按通道 POST，可指定规则', async () => {
    requestMock.mockResolvedValue([
      { channel: 'webhook', status: 'sent', attempts: 1, error: null },
      { channel: 'smtp', status: 'failed', attempts: 3, error: 'SMTP SMTPConnectError' },
    ])
    const results = await sendTestNotification('all', 7)

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/test', {
      method: 'POST',
      body: { channel: 'all', rule_id: 7 },
    })
    expect(results).toHaveLength(2)
    expect(results[1].status).toBe('failed')
  })

  it('投递历史返回服务端分页对象，筛选条件全部透传', async () => {
    requestMock.mockResolvedValue({ items: [], total: 0, page: 2, page_size: 50 })
    const page = await fetchNotificationDeliveries({
      page: 2,
      page_size: 50,
      status: 'failed',
      channel: 'smtp',
      device_id: 'dev-a',
      start: '2026-11-01T00:00:00',
      end: '2026-11-05T23:59:59',
    })

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/deliveries', {
      params: {
        page: 2,
        page_size: 50,
        status: 'failed',
        channel: 'smtp',
        device_id: 'dev-a',
        start: '2026-11-01T00:00:00',
        end: '2026-11-05T23:59:59',
      },
    })
    expect(page.total).toBe(0)
    expect(page.page_size).toBe(50)
  })

  it('未选筛选条件时不携带空参数', async () => {
    requestMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
    await fetchNotificationDeliveries()

    expect(requestMock).toHaveBeenCalledWith('/api/system/notifications/deliveries', {
      params: {
        page: 1,
        page_size: 20,
        status: undefined,
        channel: undefined,
        device_id: undefined,
        start: undefined,
        end: undefined,
      },
    })
  })
})
