import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 设备影子 API（v1.36.0）：路径、merge patch 与批量筛选条件必须原样透传。 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import {
  batchUpdateShadowDesired,
  clearDeviceShadowDesiredKeys,
  fetchDeviceShadow,
  updateDeviceShadowDesired,
} from '@/api/devices'
import type { DeviceShadow, DeviceShadowBatchResult } from '@/types'

const shadow: DeviceShadow = {
  device_id: 'dev-0001',
  desired: { report_interval: 30, led: 'on' },
  reported: { report_interval: 15 },
  version: 2,
  reported_version: 1,
  sync_status: 'pending',
  diff_keys: ['led', 'report_interval'],
  pending_desired: { report_interval: 30, led: 'on' },
  desired_updated_at: '2026-11-13T08:00:00Z',
  desired_updated_by: 'operator',
  reported_updated_at: '2026-11-13T08:01:00Z',
}

beforeEach(() => {
  requestMock.mockReset()
})

describe('设备影子 API', () => {
  it('读取影子时对设备 ID 做 URL 编码', async () => {
    requestMock.mockResolvedValue(shadow)
    const data = await fetchDeviceShadow('dev/0001')

    expect(requestMock).toHaveBeenCalledWith('/api/devices/dev%2F0001/shadow')
    expect(data.diff_keys).toEqual(['led', 'report_interval'])
  })

  it('单台写入使用 PUT，null 清除语义原样保留', async () => {
    requestMock.mockResolvedValue(shadow)
    await updateDeviceShadowDesired('dev-0001', { report_interval: 30, led: null })

    expect(requestMock).toHaveBeenCalledWith('/api/devices/dev-0001/shadow/desired', {
      method: 'PUT',
      body: { patch: { report_interval: 30, led: null } },
    })
  })

  it('清除指定键使用 DELETE 重复查询参数', async () => {
    requestMock.mockResolvedValue(shadow)
    await clearDeviceShadowDesiredKeys('dev-0001', ['led', 'fan_speed'])

    expect(requestMock).toHaveBeenCalledWith('/api/devices/dev-0001/shadow/desired', {
      method: 'DELETE',
      params: { keys: ['led', 'fan_speed'] },
    })
  })

  it('批量下发同时携带设备、标签、分组与 patch', async () => {
    const result: DeviceShadowBatchResult = {
      results: [
        { device_id: 'dev-0001', status: 'updated', version: 3, error: null },
        { device_id: 'dev-missing', status: 'not_found', version: 0, error: '设备不存在' },
      ],
      matched: 1,
      updated: 1,
      unchanged: 0,
      not_found: 1,
    }
    requestMock.mockResolvedValue(result)
    const data = await batchUpdateShadowDesired({
      device_ids: ['dev-0001', 'dev-missing'],
      tags: ['冷库'],
      group: '华东',
      patch: { report_interval: 60 },
    })

    expect(requestMock).toHaveBeenCalledWith('/api/devices/shadow/desired/batch', {
      method: 'POST',
      body: {
        device_ids: ['dev-0001', 'dev-missing'],
        tags: ['冷库'],
        group: '华东',
        patch: { report_interval: 60 },
      },
    })
    expect(data.not_found).toBe(1)
  })
})
