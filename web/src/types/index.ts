/**
 * 与《接口契约》一一对应的 TS 类型。
 *
 * 注意两类命名：
 * - REST 返回 snake_case（device_id / last_report_at / created_at ...）
 * - MQTT 报文为 camelCase（deviceId / aiGenerated / cmdId ...）
 */

// ==================== 通用 ====================

/** 统一响应包体 */
export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
}

/** 分页结果 */
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// ==================== 认证 ====================

export interface LoginBody {
  username: string
  password: string
}

export interface UserInfo {
  username: string
  role: string
}

export interface LoginResult {
  token: string
  user: UserInfo
}

// ==================== 设备 / 传感器 ====================

export type DeviceStatus = 'online' | 'offline'

/** 设备表（REST snake_case） */
export interface Device {
  device_id: string
  name: string
  model: string
  firmware_version: string
  status: DeviceStatus
  last_report_at: string | null
  last_online_at: string | null
}

/** 传感器时序记录（REST snake_case） */
export interface SensorRecord {
  id: number
  device_id: string
  temperature: number
  humidity: number
  voltage: number
  rssi: number
  battery: number
  seq: number
  ts: string
}

/** 设备详情：设备信息 + 最新一条传感器数据 */
export interface DeviceDetail extends Device {
  latest: SensorRecord | null
}

export interface DeviceListQuery {
  status?: DeviceStatus
  keyword?: string
  page?: number
  pageSize?: number
}

export interface DeviceRecordsQuery {
  /** ISO8601 字符串或毫秒时间戳 */
  start?: string | number
  end?: string | number
  limit?: number
}

// ==================== 告警 ====================

export type AlertLevel = 'warning' | 'critical'

/** 告警（REST snake_case） */
export interface AlertItem {
  alert_id: string
  device_id: string
  level: AlertLevel
  metric: string
  value: number
  threshold: number
  message: string
  ai_generated: boolean
  status: string
  created_at: string
}

/** 告警展示模型：把 REST / MQTT 两种来源统一成组件可用的结构 */
export interface AlertView {
  id: string
  deviceId: string
  deviceName?: string
  level: AlertLevel
  metric: string
  value: number
  threshold: number
  message: string
  aiGenerated: boolean
  ts: number
}

export interface AlertListQuery {
  deviceId?: string
  level?: AlertLevel
  status?: 'open' | 'acked'
  page?: number
  pageSize?: number
}

// ==================== 指令 ====================

export type CommandType = 'calibrate' | 'ota' | 'reboot' | 'config'
export type CommandState = 'issued' | 'received' | 'progress' | 'success' | 'failed'

/** 指令记录（REST snake_case） */
export interface CommandItem {
  cmd_id: string
  device_id: string
  type: CommandType
  payload: Record<string, unknown>
  state: CommandState
  progress: number
  message: string | null
  ota_task_id: string | null
  operator: string
  created_at: string
  updated_at: string | null
}

// ==================== OTA ====================

/** 创建 OTA 任务的入参：网关按别名校验，必须用 camelCase */
export interface OtaTaskCreateBody {
  deviceIds: string[]
  targetVersion: string
  url: string
  description?: string
}

/** OTA 任务下单台设备的升级状态（REST snake_case） */
export interface OtaDeviceState {
  device_id: string
  cmd_id: string
  state: CommandState
  progress: number
  message: string | null
}

/** OTA 任务（REST snake_case） */
export interface OtaTask {
  task_id: string
  target_version: string
  url: string
  description: string | null
  total: number
  success: number
  failed: number
  created_at: string
  devices: OtaDeviceState[]
}

// ==================== 统计 ====================

export interface StatsOverview {
  device_total: number
  device_online: number
  device_offline: number
  report_today: number
  alert_open: number
}

// ==================== MQTT 报文（camelCase） ====================

/** device/{deviceId}/sensor/report */
export interface SensorReportMessage {
  deviceId: string
  temperature: number
  humidity: number
  voltage: number
  rssi: number
  battery: number
  seq: number
  ts: number
}

/** device/{deviceId}/status */
export interface DeviceStatusMessage {
  deviceId: string
  status: DeviceStatus
  firmwareVersion?: string | null
  reason?: string | null
  ts: number
}

/** device/{deviceId}/cmd/ack */
export interface CommandAckMessage {
  cmdId: string
  type: string
  state: CommandState
  progress: number
  message?: string | null
  ts: number
}

/** device/system/alert */
export interface SystemAlertMessage {
  alertId: string
  deviceId: string
  deviceName?: string
  level: AlertLevel
  metric: string
  value: number
  threshold: number
  message: string
  aiGenerated: boolean
  ts: number
}
