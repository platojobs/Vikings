/** 统计概览接口 */

import { request } from '@/api/http'
import type { StatsOverview } from '@/types'

export function fetchStatsOverview(): Promise<StatsOverview> {
  return request<StatsOverview>('/api/stats/overview')
}
