import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 电池健康看板 API（v1.32.0）：
 * - 查询条件（分组 / 窗口天数 / 清单条数）原样交给服务端裁决；
 * - 不传条件时保持服务端默认值（7 天、20 台）；
 * - 响应字段直接透传，前端不自造分层口径。
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import { fetchBatteryHealth } from '@/api/battery'
import type { BatteryHealth } from '@/types'

const payload: BatteryHealth = {
  device_total: 5,
  battery_known: 4,
  critical: 2,
  warning: 1,
  healthy: 1,
  unknown: 1,
  avg_battery: 41.3,
  warning_percent: 30,
  critical_percent: 15,
  window_days: 7,
  window_start: '2026-10-30T00:00:00Z',
  trend: [{ day_start: '2026-11-05T00:00:00Z', avg_battery: 41.3, samples: 4 }],
  groups: [
    {
      group_name: '华东',
      total: 3,
      critical: 2,
      warning: 1,
      healthy: 0,
      unknown: 0,
      avg_battery: 18.3,
    },
  ],
  watchlist: [
    {
      device_id: 'dev-0001',
      name: '设备 dev-0001',
      group_name: '华东',
      status: 'online',
      battery: 10,
      voltage: 3.1,
      last_report_at: '2026-11-05T03:00:00Z',
      drain_per_day: 4.5,
      days_to_critical: 0,
      risk: 'critical',
      open_alert_count: 1,
    },
  ],
  watchlist_total: 3,
}

beforeEach(() => {
  requestMock.mockReset()
})

describe('电池健康看板 API', () => {
  it('默认查询只带服务端可识别的路径', async () => {
    requestMock.mockResolvedValue(payload)
    const data = await fetchBatteryHealth()

    expect(requestMock).toHaveBeenCalledWith('/api/stats/battery-health', {
      params: { group: undefined, days: undefined, limit: undefined },
    })
    expect(data.watchlist[0].risk).toBe('critical')
    expect(data.watchlist_total).toBe(3)
  })

  it('分组、窗口天数与清单条数原样透传', async () => {
    requestMock.mockResolvedValue(payload)
    await fetchBatteryHealth({ group: '华东', days: 14, limit: 50 })

    expect(requestMock).toHaveBeenCalledWith('/api/stats/battery-health', {
      params: { group: '华东', days: 14, limit: 50 },
    })
  })

  it('服务端报错时向上抛出，不返回空数据', async () => {
    requestMock.mockRejectedValue(new Error('网关不可用'))

    await expect(fetchBatteryHealth({ days: 7 })).rejects.toThrow('网关不可用')
  })
})
