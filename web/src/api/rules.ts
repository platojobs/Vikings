/** 告警规则接口：阈值在线查询与更新 */

import { request } from '@/api/http'
import type { ThresholdsUpdateBody, ThresholdsUpdateResult, ThresholdsView } from '@/types'

export function fetchThresholds(): Promise<ThresholdsView> {
  return request<ThresholdsView>('/api/rules/thresholds')
}

export function updateThresholds(body: ThresholdsUpdateBody): Promise<ThresholdsUpdateResult> {
  return request<ThresholdsUpdateResult>('/api/rules/thresholds', {
    method: 'PUT',
    body,
  })
}
