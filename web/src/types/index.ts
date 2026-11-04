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

/** 登录后换取的只读 MQTT 凭证（服务端 env 下发，不进构建产物） */
export interface MqttCredential {
  client: string
  username: string
  password: string
  read_only: boolean
  topics: string[]
}

// ==================== 账号与权限 ====================

/** 角色：admin 全部权限 / operator 运维执行 / viewer 只读 */
export type UserRole = 'admin' | 'operator' | 'viewer'

export interface AppUser {
  username: string
  role: UserRole
  active: boolean
  last_login_at: string | null
  created_at: string
}

export interface UserCreateBody {
  username: string
  password: string
  role: UserRole
}

export interface UserUpdateBody {
  role?: UserRole
  active?: boolean
  password?: string
}

// ==================== 设备 / 传感器 ====================

export type DeviceStatus = 'online' | 'offline'

/** 设备表（REST snake_case） */
export interface Device {
  device_id: string
  name: string
  model: string
  firmware_version: string
  group_name: string
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
  group?: string
  page?: number
  pageSize?: number
}

/** 手工注册设备 */
export interface DeviceCreateBody {
  device_id: string
  name: string
  model: string
  firmware_version: string
  group_name: string
}

/** 编辑设备元数据（设备 ID 不可改） */
export interface DeviceUpdateBody {
  name?: string
  model?: string
  firmware_version?: string
  group_name?: string
}

/** 批量注册结果 */
export interface DeviceBulkResult {
  created: string[]
  skipped: string[]
}

/** 分组概览 */
export interface DeviceGroupStat {
  group_name: string
  total: number
  online: number
  offline: number
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

/** 告警统计卡片（GET /api/alerts/summary） */
export interface AlertSummary {
  open: number
  critical_open: number
  today: number
}

// ==================== 阈值在线配置 ====================

/** 单项阈值的生效值与来源（override=在线覆盖 / env=环境变量） */
export interface ThresholdEntry {
  value: number
  source: 'override' | 'env'
}

/** GET /api/rules/thresholds 的分组视图 */
export interface ThresholdsView {
  voltage: { warning: ThresholdEntry; critical: ThresholdEntry }
  temperature: {
    warning_high: ThresholdEntry
    critical_high: ThresholdEntry
    warning_low: ThresholdEntry
    critical_low: ThresholdEntry
  }
  humidity: { warning: ThresholdEntry; critical: ThresholdEntry }
  rssi: { warning: ThresholdEntry; critical: ThresholdEntry }
}

/** PUT /api/rules/thresholds 入参：只传需要修改的键 */
export interface ThresholdsUpdateBody {
  voltage_warning?: number
  voltage_critical?: number
  temperature_warning_high?: number
  temperature_critical_high?: number
  temperature_warning_low?: number
  temperature_critical_low?: number
  humidity_warning?: number
  humidity_critical?: number
  rssi_warning?: number
  rssi_critical?: number
}

export interface ThresholdsUpdateResult {
  updated: Record<string, number>
  thresholds: ThresholdsView
}

// ==================== 审计日志 ====================

export type AuditAction =
  | 'login'
  | 'command.create'
  | 'ota.create'
  | 'alert.ack'
  | 'threshold.update'

export interface AuditLog {
  id: number
  operator: string
  action: string
  target_type: string
  target_id: string
  detail: Record<string, unknown>
  created_at: string
}

export interface AuditLogQuery {
  operator?: string
  action?: string
  page?: number
  pageSize?: number
}

// ==================== 数据聚合 ====================

export type AggregateBucket = 'hour' | 'day'

/** 时间桶聚合点（GET /api/devices/{id}/records/aggregate） */
export interface AggregatePoint {
  bucket_start: string
  count: number
  temperature_avg: number
  temperature_min: number
  temperature_max: number
  humidity_avg: number
  humidity_min: number
  humidity_max: number
  voltage_avg: number
  voltage_min: number
  voltage_max: number
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

// ==================== 系统状态 ====================

/** GET /api/system/status 的分段结构，全部为 REST snake_case */
export interface SystemGatewayStatus {
  version: string
  started_at: string
  uptime_seconds: number
}

export interface SystemMqttStatus {
  connected: boolean
  host: string
  port: number
  messages_handled: number
  last_error: string | null
}

export interface SystemStorageStatus {
  backend: string
  device_total: number
  device_online: number
  device_offline: number
  record_total: number
  record_oldest_at: string | null
  record_newest_at: string | null
  alert_total: number
  alert_open: number
}

export interface SystemSchedulerStatus {
  offline_timeout_seconds: number
  offline_sweep_interval_seconds: number
  data_retention_days: number
  data_purge_interval_seconds: number
  alert_suppress_seconds: number
}

export interface SystemNotifyStatus {
  webhook_enabled: boolean
  webhook_target: string
}

export interface SystemStatus {
  gateway: SystemGatewayStatus
  mqtt: SystemMqttStatus
  storage: SystemStorageStatus
  scheduler: SystemSchedulerStatus
  notify: SystemNotifyStatus
  generated_at: string
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
