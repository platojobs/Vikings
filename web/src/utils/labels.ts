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
