import { beforeEach, describe, expect, it, vi } from 'vitest'
import { analyzeOtaCandidates, buildOtaConfirmText, formatOtaPreflightMessage, summarizeBulkCommand } from '@/utils/batch'

/**
 * 覆盖范围（v1.28.0）：
 * - 批量指令结果按成功/失败/跳过分类，失败原因来自服务端逐台明细；
 * - OTA 预检把「未签发凭证 / 离线」提前暴露，归类互斥；
 * - 二次确认文案包含目标台数和不可逆影响。
 * 边界：不挂载 Element Plus 表格验证勾选行为，交互由 E2E 覆盖。
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }))
vi.mock('@/api/http', () => ({ request: requestMock }))

import { sendBulkCommand } from '@/api/devices'
import type { CommandBulkResult, Device } from '@/types'

const result: CommandBulkResult = {
  batch_id: 'cmdbatch-abc',
  type: 'config',
  requested: 4,
  replayed: false,
  success: [{ device_id: 'dev-1', cmd_id: 'cmd-1' }],
  failed: [{ device_id: 'dev-2', reason: 'MQTT 发布失败' }],
  skipped: [
    { device_id: 'dev-3', reason: '设备离线，指令无法投递（MQTT 非持久会话）' },
    { device_id: 'dev-4', reason: '未签发有效 MQTT 凭证，设备无法接入 Broker' },
  ],
}

function device(overrides: Partial<Device>): Device {
  return {
    device_id: 'dev-x',
    name: '设备',
    model: 'VG-S100',
    firmware_version: '1.0.0',
    group_name: '默认分组',
    status: 'online',
    last_report_at: null,
    last_online_at: null,
    ...overrides,
  }
}

beforeEach(() => {
  requestMock.mockReset()
})

describe('批量指令 API', () => {
  it('提交 device_ids / type / payload / request_id', async () => {
    requestMock.mockResolvedValue(result)
    const data = await sendBulkCommand(['dev-1', 'dev-2'], 'config', { reportIntervalSec: 30 }, 'req-12345678')

    expect(requestMock).toHaveBeenCalledWith('/api/devices/commands/bulk', {
      method: 'POST',
      body: {
        device_ids: ['dev-1', 'dev-2'],
        type: 'config',
        payload: { reportIntervalSec: 30 },
        request_id: 'req-12345678',
      },
    })
    expect(data).toEqual(result)
  })
})

describe('批量结果摘要', () => {
  it('按成功/失败/跳过分组并保留具体原因', () => {
    const summary = summarizeBulkCommand(result)
    expect(summary.groups.map((group) => [group.key, group.count])).toEqual([
      ['success', 1],
      ['failed', 1],
      ['skipped', 2],
    ])
    expect(summary.groups[1].items[0].detail).toBe('MQTT 发布失败')
    expect(summary.groups[2].items[1].detail).toContain('凭证')
  })

  it('重复提交提示未重复下发', () => {
    const summary = summarizeBulkCommand({ ...result, replayed: true })
    expect(summary.replayed).toBe(true)
    expect(summary.title).toContain('未重复下发')
  })
})

describe('OTA 预检', () => {
  it('未签发凭证优先于离线归类；在线设备不因元数据缺失被误判', () => {
    const preflight = analyzeOtaCandidates([
      device({ device_id: 'ok', mqtt_credential_status: 'active' }),
      device({ device_id: 'online-missing', mqtt_credential_status: 'missing' }),
      device({ device_id: 'offline', status: 'offline', mqtt_credential_status: 'active' }),
      device({ device_id: 'nocred', status: 'offline', mqtt_credential_status: 'missing' }),
      device({ device_id: 'revoked', mqtt_credential_status: 'revoked' }),
    ])
    expect(preflight.ready.map((item) => item.device_id)).toEqual(['ok', 'online-missing'])
    expect(preflight.offline.map((item) => item.device_id)).toEqual(['offline'])
    expect(preflight.uncredentialed.map((item) => item.device_id)).toEqual(['nocred', 'revoked'])
    expect(preflight.blockedCount).toBe(3)
  })

  it('预检与确认文案写清风险台数与不可逆影响', () => {
    const preflight = analyzeOtaCandidates([
      device({ device_id: 'ok', mqtt_credential_status: 'active' }),
      device({ device_id: 'offline-1', status: 'offline', mqtt_credential_status: 'active' }),
      device({ device_id: 'nocred-1', status: 'offline', mqtt_credential_status: 'missing' }),
    ])
    expect(formatOtaPreflightMessage(preflight)).toContain('1 台离线')
    expect(formatOtaPreflightMessage(preflight)).toContain('1 台未签发有效凭证')
    const text = buildOtaConfirmText('1.2.0', 3, preflight)
    expect(text).toContain('3 台设备')
    expect(text).toContain('不能撤回')
  })

  it('全部就绪时不误报风险', () => {
    const preflight = analyzeOtaCandidates([
      device({ device_id: 'ok', mqtt_credential_status: 'active' }),
    ])
    expect(preflight.blockedCount).toBe(0)
    expect(formatOtaPreflightMessage(preflight)).toContain('预计可正常接收升级指令')
  })
})
