/** 设备与传感器数据接口 */

import { request } from '@/api/http'
import type {
  CommandItem,
  CommandType,
  Device,
  DeviceDetail,
  DeviceListQuery,
  DeviceRecordsQuery,
  PageResult,
  SensorRecord,
} from '@/types'

export function fetchDevices(query: DeviceListQuery = {}): Promise<PageResult<Device>> {
  return request<PageResult<Device>>('/api/devices', {
    params: {
      status: query.status,
      keyword: query.keyword,
      page: query.page,
      pageSize: query.pageSize,
    },
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
