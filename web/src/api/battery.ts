/** 电池健康与续航看板接口（v1.32.0） */

import { request } from '@/api/http'
import type { BatteryHealth, BatteryHealthQuery } from '@/types'

export function fetchBatteryHealth(query: BatteryHealthQuery = {}): Promise<BatteryHealth> {
  return request<BatteryHealth>('/api/stats/battery-health', {
    params: {
      group: query.group,
      days: query.days,
      limit: query.limit,
    },
  })
}
