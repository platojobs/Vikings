import { beforeEach, describe, expect, it, vi } from 'vitest'
import { summarizeBulkAck } from '@/utils/alertSummary'

/**
 * 覆盖范围与边界（v1.21.0）：
 * - 覆盖 api 层的请求路径/方法/body（含备注与不传备注两种旧客户端路径）；
 * - 覆盖批量确认结果摘要的拼装（部分失败必须逐类可见）。
 * - 边界：不挂载 Element Plus 表格做 DOM 级勾选测试（成本高且依赖样式），
 *   多选交互由 Flutter widget 测试 + E2E 批量确认步骤覆盖，Web 仅验证数据与文案。
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import {
  ackAlert,
  bulkAckAlerts,
  fetchAlerts,
  fetchSlaConfig,
  handoverAlerts,
  updateSlaConfig,
} from '@/api/alerts'
import type { BulkAckResult } from '@/types'

const result: BulkAckResult = {
  acked: ['alt-1', 'alt-2'],
  already_acked: ['alt-3'],
  not_found: ['alt-missing'],
  requested: 4,
}

beforeEach(() => {
  requestMock.mockReset()
})

describe('告警确认 API', () => {
  it('批量确认复用 bulk-ack 且带备注', async () => {
    requestMock.mockResolvedValue(result)
    const data = await bulkAckAlerts(['alt-1', 'alt-2', 'alt-3'], '  已联系现场  ')

    expect(requestMock).toHaveBeenCalledWith('/api/alerts/bulk-ack', {
      method: 'POST',
      body: { alert_ids: ['alt-1', 'alt-2', 'alt-3'], note: '已联系现场' },
    })
    expect(data).toEqual(result)
  })

  it('旧客户端不传备注时 body 只含 alert_ids', async () => {
    requestMock.mockResolvedValue(result)
    await bulkAckAlerts(['alt-1'])

    expect(requestMock).toHaveBeenCalledWith('/api/alerts/bulk-ack', {
      method: 'POST',
      body: { alert_ids: ['alt-1'] },
    })
  })

  it('单条确认带备注写入 body', async () => {
    requestMock.mockResolvedValue({})
    await ackAlert('alt-1', ' 重启设备后恢复 ')

    expect(requestMock).toHaveBeenCalledWith('/api/alerts/alt-1/ack', {
      method: 'POST',
      body: { note: '重启设备后恢复' },
    })
  })

  it('单条确认不传备注时保持无 body，兼容旧调用', async () => {
    requestMock.mockResolvedValue({})
    await ackAlert('alt-1')

    expect(requestMock).toHaveBeenCalledWith('/api/alerts/alt-1/ack', {
      method: 'POST',
      body: undefined,
    })
  })
})

describe('告警 SLA 与值班交接 API（v1.33.0）', () => {
  it('列表把 sla 筛选下推到后端', async () => {
    requestMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 })
    await fetchAlerts({ sla: 'overdue', page: 1, pageSize: 10 })

    expect(requestMock).toHaveBeenCalledWith('/api/alerts', {
      params: {
        deviceId: undefined,
        level: undefined,
        status: undefined,
        sla: 'overdue',
        start: undefined,
        end: undefined,
        page: 1,
        pageSize: 10,
      },
    })
  })

  it('交接只传目标与可选范围，不夹带页面状态', async () => {
    requestMock.mockResolvedValue({
      from_user: 'alice', to: 'next-shift', count: 2,
      window_start: null, window_end: null, notification: 'queued',
    })
    const data = await handoverAlerts({ to: 'next-shift', from_user: 'alice', include_unassigned: true })

    expect(requestMock).toHaveBeenCalledWith('/api/alerts/handover', {
      method: 'POST',
      body: { to: 'next-shift', from_user: 'alice', include_unassigned: true },
    })
    expect(data.count).toBe(2)
  })

  it('SLA 配置读取 GET / 保存 PUT 使用同一路径', async () => {
    requestMock.mockResolvedValue({
      critical_minutes: 5, warning_minutes: 60,
      max_escalations: 1, escalation_interval_minutes: 30, updated_by: 'admin',
    })
    await fetchSlaConfig()
    expect(requestMock).toHaveBeenCalledWith('/api/alerts/sla-config')

    requestMock.mockClear()
    await updateSlaConfig({ critical_minutes: 5, warning_minutes: 60, max_escalations: 1 })
    expect(requestMock).toHaveBeenCalledWith('/api/alerts/sla-config', {
      method: 'PUT',
      body: { critical_minutes: 5, warning_minutes: 60, max_escalations: 1 },
    })
  })
})

describe('批量确认结果摘要', () => {
  it('全部成功 → success，并说明已写入处置信息', () => {
    const summary = summarizeBulkAck({
      acked: ['alt-1'],
      already_acked: [],
      not_found: [],
      requested: 1,
    })
    expect(summary.severity).toBe('success')
    expect(summary.title).toContain('已确认 1 条')
    expect(summary.detail).toContain('操作人')
  })

  it('部分失败 → warning，逐类列出不存在与已确认', () => {
    const summary = summarizeBulkAck(result)
    expect(summary.severity).toBe('warning')
    expect(summary.title).toContain('已确认 2 条')
    expect(summary.title).toContain('1 条此前已确认')
    expect(summary.title).toContain('1 条不存在')
    expect(summary.detail).toContain('alt-missing')
    expect(summary.detail).toContain('保留原操作人')
  })

  it('全部不存在 → error，不伪装成成功', () => {
    const summary = summarizeBulkAck({
      acked: [],
      already_acked: [],
      not_found: ['alt-a', 'alt-b'],
      requested: 2,
    })
    expect(summary.severity).toBe('error')
    expect(summary.title).toContain('失败')
    expect(summary.detail).toContain('alt-a')
  })

  it('全部此前已确认 → warning，且不覆盖首次处置信息', () => {
    const summary = summarizeBulkAck({
      acked: [],
      already_acked: ['alt-1'],
      not_found: [],
      requested: 1,
    })
    expect(summary.severity).toBe('warning')
    expect(summary.title).toContain('此前已确认')
    expect(summary.detail).toContain('本次未覆盖')
  })

  it('空结果 → warning，提示没有有效告警', () => {
    const summary = summarizeBulkAck({
      acked: [],
      already_acked: [],
      not_found: [],
      requested: 0,
    })
    expect(summary.title).toBe('没有可确认的告警')
  })

  it('不存在 ID 超过 5 条时折叠展示', () => {
    const summary = summarizeBulkAck({
      acked: ['alt-1'],
      already_acked: [],
      not_found: ['a', 'b', 'c', 'd', 'e', 'f'],
      requested: 7,
    })
    expect(summary.detail).toContain('等 6 条')
  })
})
