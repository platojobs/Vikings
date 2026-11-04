/**
 * 批量确认结果摘要（v1.21.0）。
 *
 * 为什么单独抽成纯函数：部分失败必须逐类可见，不能一句「操作成功」糊过去；
 * 这里把 acked / already_acked / not_found 拼成稳定的标题与明细，
 * 既供页面渲染，也便于 vitest 直接覆盖拼装逻辑。
 */

import type { BulkAckResult } from '@/types'

export type BulkAckSeverity = 'success' | 'warning' | 'error'

export interface BulkAckSummary {
  severity: BulkAckSeverity
  title: string
  detail: string
}

const MAX_LISTED_IDS = 5

function formatIds(ids: string[]): string {
  if (ids.length <= MAX_LISTED_IDS) return ids.join('、')
  return `${ids.slice(0, MAX_LISTED_IDS).join('、')} 等 ${ids.length} 条`
}

export function summarizeBulkAck(result: BulkAckResult): BulkAckSummary {
  const acked = result.acked.length
  const already = result.already_acked.length
  const missing = result.not_found.length

  const counts = [
    acked ? `已确认 ${acked} 条` : '',
    already ? `${already} 条此前已确认` : '',
    missing ? `${missing} 条不存在` : '',
  ]
    .filter(Boolean)
    .join('；')

  const detailParts: string[] = []
  if (missing) {
    detailParts.push(`未找到的告警 ID：${formatIds(result.not_found)}`)
  }
  if (already) {
    detailParts.push('此前已确认的告警保留原操作人、时间与备注，本次未覆盖')
  }
  if (acked) {
    detailParts.push('已确认的告警已写入当前操作人、确认时间与本次备注')
  }

  let severity: BulkAckSeverity = 'success'
  let title = '批量确认完成'
  if (missing > 0) {
    severity = acked > 0 ? 'warning' : 'error'
    title = acked > 0 ? '批量确认部分成功' : `批量确认失败：${missing} 条告警不存在`
  } else if (acked === 0) {
    severity = 'warning'
    title = '本次没有新的确认'
  }

  if (!counts) {
    return {
      severity: 'warning',
      title: '没有可确认的告警',
      detail: '请求中没有有效告警 ID，或选中的告警已全部处理。',
    }
  }

  return {
    severity,
    title: `${title}（${counts}）`,
    detail: detailParts.join('；') || '全部告警均已完成确认。',
  }
}
