/** 指令 / 任务状态的统一中文文案与标签颜色 */

import type { CommandState } from '@/types'

const COMMAND_STATE_LABELS: Record<CommandState, string> = {
  issued: '已下发',
  received: '设备已接收',
  progress: '升级中',
  success: '成功',
  failed: '失败',
}

export function commandStateLabel(state: string): string {
  return COMMAND_STATE_LABELS[state as CommandState] ?? state
}

export type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

export function commandStateTagType(state: string): TagType {
  switch (state) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    case 'progress':
      return 'warning'
    case 'received':
      return 'primary'
    default:
      return 'info'
  }
}

/** el-progress 状态：成功绿 / 失败红 / 其余默认蓝 */
export function progressStatus(state: string): '' | 'success' | 'exception' {
  if (state === 'success') return 'success'
  if (state === 'failed') return 'exception'
  return ''
}

// ==================== 告警 ====================

export function alertLevelLabel(level: string): string {
  return level === 'critical' ? '严重' : '警告'
}

export function alertLevelTagType(level: string): TagType {
  return level === 'critical' ? 'danger' : 'warning'
}

export function alertStatusLabel(status: string): string {
  return status === 'acked' ? '已确认' : '未处理'
}

/** 告警指标中文名 */
export function alertMetricLabel(metric: string): string {
  const labels: Record<string, string> = {
    temperature: '温度',
    humidity: '湿度',
    voltage: '电压',
    rssi: '信号强度',
    battery: '电量',
  }
  return labels[metric] ?? metric
}

// ==================== 审计 ====================

export function auditActionLabel(action: string): string {
  const labels: Record<string, string> = {
    login: '登录',
    'command.create': '下发指令',
    'ota.create': '创建 OTA 任务',
    'alert.ack': '确认告警',
    'threshold.update': '修改阈值',
    'user.create': '新建账号',
    'user.update': '修改账号',
    'user.delete': '删除账号',
    'device.create': '注册设备',
    'device.update': '编辑设备',
    'device.delete': '删除设备',
    'device.bulk_create': '批量注册设备',
  }
  return labels[action] ?? action
}

export function auditActionTagType(action: string): TagType {
  switch (action) {
    case 'login':
      return 'info'
    case 'alert.ack':
      return 'warning'
    case 'threshold.update':
      return 'danger'
    case 'user.create':
    case 'user.update':
    case 'user.delete':
      return 'danger'
    case 'device.create':
    case 'device.bulk_create':
      return 'success'
    case 'device.update':
      return 'primary'
    case 'device.delete':
      return 'danger'
    default:
      return 'primary'
  }
}

// ==================== 账号角色 ====================

export function userRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    admin: '管理员',
    operator: '运维',
    viewer: '只读',
  }
  return labels[role] ?? role
}

export function userRoleTagType(role: string): TagType {
  switch (role) {
    case 'admin':
      return 'danger'
    case 'operator':
      return 'primary'
    default:
      return 'info'
  }
}
