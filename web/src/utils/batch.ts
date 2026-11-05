/**
 * 批量操作摘要与 OTA 预检（v1.28.0）。
 *
 * 这里只放纯函数：批量结果分类、OTA 目标设备风险预检、确认文案拼装。
 * 交互层（表格多选、弹窗）由视图负责；Web 单测覆盖数据与文案，DOM 勾选由 E2E 覆盖。
 */

import type { BulkCommandType, CommandBulkResult, Device } from '@/types'

/** 与服务端 CommandBulkRequest / OtaTaskCreateRequest 的上限保持一致 */
export const BULK_DEVICE_LIMIT = 200

export function bulkCommandLabel(type: BulkCommandType): string {
  switch (type) {
    case 'calibrate':
      return '传感器校准'
    case 'config':
      return '运行参数配置'
    case 'reboot':
      return '设备重启'
  }
}

export interface BulkResultGroup {
  key: 'success' | 'failed' | 'skipped'
  label: string
  count: number
  items: { device_id: string; detail: string }[]
}

export interface BulkResultSummary {
  batchId: string
  replayed: boolean
  title: string
  groups: BulkResultGroup[]
}

/**
 * 把批量指令结果拼成「成功 / 失败 / 跳过」三组，逐台可见。
 * 失败与跳过必须带服务端返回的具体原因，不能只报总数。
 */
export function summarizeBulkCommand(result: CommandBulkResult): BulkResultSummary {
  const label = bulkCommandLabel(result.type)
  return {
    batchId: result.batch_id,
    replayed: result.replayed,
    title: result.replayed
      ? `${label}：本次为重复提交，已返回首次结果（未重复下发）`
      : `${label}：成功 ${result.success.length} 台 / 失败 ${result.failed.length} 台 / 跳过 ${result.skipped.length} 台`,
    groups: [
      {
        key: 'success',
        label: '成功',
        count: result.success.length,
        items: result.success.map((item) => ({
          device_id: item.device_id,
          detail: `指令 ${item.cmd_id} 已下发`,
        })),
      },
      {
        key: 'failed',
        label: '失败',
        count: result.failed.length,
        items: result.failed.map((item) => ({
          device_id: item.device_id,
          detail: item.reason,
        })),
      },
      {
        key: 'skipped',
        label: '跳过',
        count: result.skipped.length,
        items: result.skipped.map((item) => ({
          device_id: item.device_id,
          detail: item.reason,
        })),
      },
    ],
  }
}

export interface OtaPreflight {
  ready: Device[]
  offline: Device[]
  uncredentialed: Device[]
  /** 离线或未签发凭证的台数（两者互斥归类，不会重复计数） */
  blockedCount: number
}

function hasActiveCredential(device: Device): boolean {
  const status = device.mqtt_credential_status
  if (status) return status === 'active'
  return Boolean(device.mqtt_credential_issued_at)
}

/**
 * OTA 提交前预检：未签发凭证或离线的设备提前暴露，而不是静默失败。
 * 只对「确实不可用」的设备告警：
 * - 已吊销凭证：无论在线状态都不可用；
 * - 离线且无有效凭证元数据；
 * - 在线设备必然已接入 Broker，即使 DB 元数据缺失（命令行签发）也视为就绪，
 *   避免历史部署被误判。
 */
export function analyzeOtaCandidates(devices: Device[]): OtaPreflight {
  const ready: Device[] = []
  const offline: Device[] = []
  const uncredentialed: Device[] = []
  for (const device of devices) {
    const credentialStatus = device.mqtt_credential_status
    if (credentialStatus === 'revoked') {
      uncredentialed.push(device)
    } else if (device.status === 'online') {
      ready.push(device)
    } else if (!hasActiveCredential(device)) {
      uncredentialed.push(device)
    } else {
      offline.push(device)
    }
  }
  return {
    ready,
    offline,
    uncredentialed,
    blockedCount: offline.length + uncredentialed.length,
  }
}

/** 预检提示文案：列出前 3 台问题设备，避免弹窗被长列表撑爆 */
export function formatOtaPreflightMessage(preflight: OtaPreflight): string {
  if (preflight.blockedCount === 0) {
    return '所选设备均已在线且持有有效凭证，预计可正常接收升级指令。'
  }
  const parts: string[] = []
  if (preflight.offline.length) {
    parts.push(
      `${preflight.offline.length} 台离线（${preflight.offline
        .slice(0, 3)
        .map((device) => device.device_id)
        .join('、')}${preflight.offline.length > 3 ? '…' : ''}）`,
    )
  }
  if (preflight.uncredentialed.length) {
    parts.push(
      `${preflight.uncredentialed.length} 台未签发有效凭证（${preflight.uncredentialed
        .slice(0, 3)
        .map((device) => device.device_id)
        .join('、')}${preflight.uncredentialed.length > 3 ? '…' : ''}）`,
    )
  }
  return `其中 ${parts.join('，')}。这些设备可能无法接收指令或等待超时后失败，建议先完成凭证签发与上线。`
}

/** OTA 提交二次确认：必须写清目标台数与不可逆影响 */
export function buildOtaConfirmText(
  targetVersion: string,
  selectedCount: number,
  preflight: OtaPreflight,
): string {
  const lines = [
    `将向 ${selectedCount} 台设备下发 OTA 任务，目标版本 ${targetVersion}。`,
    formatOtaPreflightMessage(preflight),
    '升级失败可能导致设备短时不可用；任务下发后不能撤回，请在维护窗口执行，并确认固件 URL 可达。',
  ]
  return lines.join('\n')
}
