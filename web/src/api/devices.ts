/** 设备与传感器数据接口 */

import { request } from '@/api/http'
import type {
  AggregateBucket,
  AggregatePoint,
  CommandItem,
  CommandType,
  Device,
  DeviceBulkResult,
  DeviceCreateBody,
  DeviceDetail,
  DeviceGroupStat,
  DeviceListQuery,
  DeviceRecordsQuery,
  DeviceUpdateBody,
  PageResult,
  SensorRecord,
} from '@/types'

export function fetchDevices(query: DeviceListQuery = {}): Promise<PageResult<Device>> {
  return request<PageResult<Device>>('/api/devices', {
    params: {
      status: query.status,
      keyword: query.keyword,
      group: query.group,
      page: query.page,
      pageSize: query.pageSize,
    },
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
