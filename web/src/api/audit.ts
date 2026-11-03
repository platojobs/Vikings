/** 审计日志接口 */

import { request } from '@/api/http'
import type { AuditLog, AuditLogQuery, PageResult } from '@/types'

export function fetchAuditLogs(query: AuditLogQuery = {}): Promise<PageResult<AuditLog>> {
  return request<PageResult<AuditLog>>('/api/audit-logs', {
    params: {
      operator: query.operator,
      action: query.action,
      page: query.page,
      pageSize: query.pageSize,
    },
  })
}
