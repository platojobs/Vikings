/** 设备与传感器数据接口 */

import { request } from '@/api/http'
import type {
  AggregateBucket,
  AggregatePoint,
  BulkCommandType,
  CommandBulkResult,
  CommandItem,
  CommandType,
  Device,
  DeviceBulkResult,
  DeviceCreateBody,
  DeviceDetail,
  DeviceGroupStat,
  DeviceListQuery,
  DeviceRecordsQuery,
  DeviceShadow,
  DeviceShadowBatchResult,
  DeviceShadowHistory,
  DeviceShadowValue,
  DeviceTagBatchResult,
  DeviceTagStat,
  DeviceUpdateBody,
  MqttCredentialResult,
  MqttCredentialRevokeResult,
  PageResult,
  SensorRecord,
} from '@/types'

export function fetchDevices(query: DeviceListQuery = {}): Promise<PageResult<Device>> {
  return request<PageResult<Device>>('/api/devices', {
    params: {
      status: query.status,
      keyword: query.keyword,
      group: query.group,
      // v1.35.0：多个标签按重复参数下发（tag=a&tag=b），服务端按「或」匹配
      tag: query.tag,
      page: query.page,
      pageSize: query.pageSize,
    },
  })
}

/** v1.35.0 标签列表（含每个标签下的设备数），登录即可读 */
export function fetchDeviceTags(): Promise<DeviceTagStat[]> {
  return request<DeviceTagStat[]>('/api/devices/tags')
}

/**
 * v1.35.0 批量打标签/移除标签（仅 admin）。
 * 返回逐设备结果：updated / unchanged / not_found / over_limit，调用方必须逐类展示。
 */
export function batchUpdateDeviceTags(body: {
  device_ids: string[]
  add?: string[]
  remove?: string[]
}): Promise<DeviceTagBatchResult> {
  return request<DeviceTagBatchResult>('/api/devices/tags/batch', {
    method: 'POST',
    body,
  })
}

/** 分组概览：每组设备数与在线/离线数量 */
export function fetchDeviceGroups(): Promise<DeviceGroupStat[]> {
  return request<DeviceGroupStat[]>('/api/devices/groups')
}

export function createDevice(body: DeviceCreateBody): Promise<Device> {
  return request<Device>('/api/devices', { method: 'POST', body })
}

export function updateDevice(deviceId: string, body: DeviceUpdateBody): Promise<Device> {
  return request<Device>(`/api/devices/${encodeURIComponent(deviceId)}`, {
    method: 'PATCH',
    body,
  })
}

export function deleteDevice(deviceId: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(`/api/devices/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
  })
}

/** 批量注册：重复设备 ID 会被跳过，返回成功与跳过明细 */
export function bulkCreateDevices(devices: DeviceCreateBody[]): Promise<DeviceBulkResult> {
  return request<DeviceBulkResult>('/api/devices/bulk', {
    method: 'POST',
    body: { devices },
  })
}

export function fetchDeviceDetail(deviceId: string): Promise<DeviceDetail> {
  return request<DeviceDetail>(`/api/devices/${encodeURIComponent(deviceId)}`)
}

export function fetchDeviceRecords(
  deviceId: string,
  query: DeviceRecordsQuery = {},
): Promise<SensorRecord[]> {
  return request<SensorRecord[]>(`/api/devices/${encodeURIComponent(deviceId)}/records`, {
    params: { start: query.start, end: query.end, limit: query.limit },
  })
}

/** 全局每台设备的最新一条数据，仪表盘首屏兜底 */
export function fetchLatestRecords(): Promise<SensorRecord[]> {
  return request<SensorRecord[]>('/api/records/latest')
}

/** 按小时/天聚合设备时序数据，用于长周期趋势，避免拉全量明细 */
export function fetchDeviceAggregate(
  deviceId: string,
  bucket: AggregateBucket,
  query: DeviceRecordsQuery = {},
): Promise<AggregatePoint[]> {
  return request<AggregatePoint[]>(
    `/api/devices/${encodeURIComponent(deviceId)}/records/aggregate`,
    {
      params: { bucket, start: query.start, end: query.end },
    },
  )
}

export function fetchDeviceCommands(
  deviceId: string,
  limit = 50,
): Promise<CommandItem[]> {
  return request<CommandItem[]>(`/api/devices/${encodeURIComponent(deviceId)}/commands`, {
    params: { limit },
  })
}

/**
 * 签发/轮换设备 MQTT 凭证。
 * 口令只在响应中出现一次；首次签发 operator 可用，覆盖已有有效凭证仅 admin。
 */
export function issueDeviceCredential(deviceId: string): Promise<MqttCredentialResult> {
  return request<MqttCredentialResult>(
    `/api/devices/${encodeURIComponent(deviceId)}/mqtt-credential`,
    { method: 'POST' },
  )
}

/** 吊销设备 MQTT 凭证（仅 admin），吊销后须重新签发才能接入 Broker。 */
export function revokeDeviceCredential(deviceId: string): Promise<MqttCredentialRevokeResult> {
  return request<MqttCredentialRevokeResult>(
    `/api/devices/${encodeURIComponent(deviceId)}/mqtt-credential`,
    { method: 'DELETE' },
  )
}

/** 下发单条指令（calibrate / reboot / config），OTA 走专用任务接口。 */
export function sendCommand(
  deviceId: string,
  type: CommandType,
  payload: Record<string, unknown> = {},
): Promise<CommandItem> {
  return request<CommandItem>(`/api/devices/${encodeURIComponent(deviceId)}/commands`, {
    method: 'POST',
    body: { type, payload },
  })
}

/**
 * 批量下发同一指令（v1.28.0）：服务端逐台预检与下发，返回成功/失败/跳过明细。
 * OTA 不在该端点受理，必须走 `/api/ota/tasks`。
 * `requestId` 用于双击/网络重试去重：同 ID 在 10 分钟内返回首次结果。
 */
export function sendBulkCommand(
  deviceIds: string[],
  type: BulkCommandType,
  payload: Record<string, unknown> = {},
  requestId?: string,
): Promise<CommandBulkResult> {
  return request<CommandBulkResult>('/api/devices/commands/bulk', {
    method: 'POST',
    body: { device_ids: deviceIds, type, payload, request_id: requestId },
  })
}

// ==================== 设备影子（v1.36.0） ====================

export function fetchDeviceShadow(deviceId: string): Promise<DeviceShadow> {
  return request<DeviceShadow>(`/api/devices/${encodeURIComponent(deviceId)}/shadow`)
}

/** merge 语义写入期望状态；值为 null 表示删除该键 */
export function updateDeviceShadowDesired(
  deviceId: string,
  patch: Record<string, DeviceShadowValue>,
): Promise<DeviceShadow> {
  return request<DeviceShadow>(`/api/devices/${encodeURIComponent(deviceId)}/shadow/desired`, {
    method: 'PUT',
    body: { patch },
  })
}

export function clearDeviceShadowDesiredKeys(
  deviceId: string,
  keys: string[],
): Promise<DeviceShadow> {
  return request<DeviceShadow>(`/api/devices/${encodeURIComponent(deviceId)}/shadow/desired`, {
    method: 'DELETE',
    params: { keys },
  })
}

/** 批量设置期望：显式设备、标签与分组同时提供时由服务端取交集 */
export function batchUpdateShadowDesired(body: {
  device_ids?: string[]
  tags?: string[]
  group?: string
  patch: Record<string, DeviceShadowValue>
}): Promise<DeviceShadowBatchResult> {
  return request<DeviceShadowBatchResult>('/api/devices/shadow/desired/batch', {
    method: 'POST',
    body,
  })
}

// ==================== 设备影子历史与回滚（v1.37.0） ====================

export function fetchDeviceShadowHistory(
  deviceId: string,
  query: { limit?: number; offset?: number } = {},
): Promise<DeviceShadowHistory> {
  return request<DeviceShadowHistory>(
    `/api/devices/${encodeURIComponent(deviceId)}/shadow/history`,
    { params: { limit: query.limit ?? 20, offset: query.offset ?? 0 } },
  )
}

/** 精确恢复目标完整快照；服务端生成新版本，不覆盖历史。 */
export function rollbackDeviceShadow(deviceId: string, version: number): Promise<DeviceShadow> {
  return request<DeviceShadow>(
    `/api/devices/${encodeURIComponent(deviceId)}/shadow/rollback`,
    { method: 'POST', body: { version } },
  )
}
