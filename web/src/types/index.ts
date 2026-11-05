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
  /** 短 TTL 访问令牌（默认 20 分钟），用于 Authorization: Bearer */
  token: string
  /** 可吊销刷新令牌；单次使用，刷新成功后返回新的 refresh_token */
  refresh_token?: string
  token_type?: string
  /** 访问令牌有效期（秒），由服务端下发 */
  expires_in?: number
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
export type MqttCredentialStatus = 'missing' | 'active' | 'revoked'

export interface Device {
  device_id: string
  name: string
  model: string
  firmware_version: string
  group_name: string
  status: DeviceStatus
  last_report_at: string | null
  last_online_at: string | null
  /** 凭证元数据：只含状态与短前缀，不含口令或完整指纹 */
  mqtt_username?: string | null
  mqtt_credential_version?: number
  mqtt_credential_issued_at?: string | null
  mqtt_credential_rotated_at?: string | null
  mqtt_credential_revoked_at?: string | null
  mqtt_credential_status?: MqttCredentialStatus
  mqtt_credential_fingerprint_prefix?: string | null
}

/** 签发/轮换结果：password 只出现这一次 */
export interface MqttCredentialResult {
  device_id: string
  username: string
  password: string
  version: number
  issued_at: string
  rotated_at: string
  operation: 'issue' | 'rotate'
  reloaded: boolean
  reload_detail: string
}

/** 吊销结果：不含口令与完整指纹 */
export interface MqttCredentialRevokeResult {
  device_id: string
  username: string
  version: number
  revoked_at: string
  reloaded: boolean
  reload_detail: string
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

/**
 * 级别口径（P2-1 / v1.21.0）：规则引擎按阈值真实产出 warning / critical，
 * 对外枚举同步只保留这两级，页面不再展示空级别。
 */
export type AlertLevel = 'warning' | 'critical'

/** 告警（REST snake_case）；v1.21.0 起带处置元数据 */
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
  /** 处置备注；旧数据/未填写为 null */
  ack_note?: string | null
  /** 确认人用户名；v1.21.0 之前的历史确认行为 null */
  acked_by?: string | null
  /** 确认时间（ISO8601）；历史确认行为 null */
  acked_at?: string | null
}

/** 批量确认结果：三类 ID 逐类返回，部分失败对调用方可见 */
export interface BulkAckResult {
  acked: string[]
  already_acked: string[]
  not_found: string[]
  requested: number
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
  /** 创建时间下限/上限（毫秒时间戳），v1.30.0 与导出口径一致 */
  start?: number
  end?: number
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

/** 批量指令可用的类型：OTA 有独立任务流程，不在批量指令端点受理 */
export type BulkCommandType = 'calibrate' | 'config' | 'reboot'

/** 批量指令逐台成功项（REST snake_case） */
export interface CommandBulkItem {
  device_id: string
  cmd_id: string
}

/** 批量指令逐台失败/跳过项，reason 可直接展示 */
export interface CommandBulkFailure {
  device_id: string
  reason: string
}

/** 批量指令结果：成功 / 失败 / 跳过 三类 */
export interface CommandBulkResult {
  batch_id: string
  type: BulkCommandType
  requested: number
  replayed: boolean
  success: CommandBulkItem[]
  failed: CommandBulkFailure[]
  skipped: CommandBulkFailure[]
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

// ==================== 电池健康与续航（v1.32.0） ====================

export type BatteryRisk = 'critical' | 'warning' | 'healthy' | 'unknown'

export interface BatteryTrendPoint {
  day_start: string
  avg_battery: number
  samples: number
}

export interface BatteryRiskDevice {
  device_id: string
  name: string
  group_name: string
  status: DeviceStatus
  battery: number | null
  voltage: number | null
  last_report_at: string | null
  /** 百分点/天；稳定或回升时为 null */
  drain_per_day: number | null
  /** 到严重阈值（15%）的预计天数；无法估算时为 null */
  days_to_critical: number | null
  risk: BatteryRisk
  open_alert_count: number
}

export interface BatteryGroupSummary {
  group_name: string
  total: number
  critical: number
  warning: number
  healthy: number
  unknown: number
  avg_battery: number | null
}

/** GET /api/stats/battery-health */
export interface BatteryHealth {
  device_total: number
  battery_known: number
  critical: number
  warning: number
  healthy: number
  unknown: number
  avg_battery: number | null
  warning_percent: number
  critical_percent: number
  window_days: number
  window_start: string
  trend: BatteryTrendPoint[]
  groups: BatteryGroupSummary[]
  watchlist: BatteryRiskDevice[]
  watchlist_total: number
}

export interface BatteryHealthQuery {
  group?: string
  days?: number
  limit?: number
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

// ==================== 告警通知（v1.29.0） ====================

/** 通道配置对外视图：URL/密钥/口令只返回「是否已配置」，不回显明文 */
export interface NotificationConfig {
  enabled: boolean
  webhook_enabled: boolean
  webhook_url_configured: boolean
  webhook_secret_configured: boolean
  webhook_timeout_seconds: number
  webhook_max_attempts: number
  smtp_enabled: boolean
  smtp_host: string | null
  smtp_port: number
  smtp_username: string | null
  smtp_password_configured: boolean
  smtp_from: string | null
  smtp_to: string | null
  smtp_starttls: boolean
  smtp_timeout_seconds: number
  smtp_max_attempts: number
  levels: string[]
  groups: string[]
  device_allow: string[]
  device_deny: string[]
  mute_minutes: number
  retry_backoff_seconds: number
  updated_at: string | null
  updated_by: string
}

/** 更新入参：敏感字段空串表示清空，不传表示保持原值 */
export type NotificationConfigPatch = Partial<{
  enabled: boolean
  webhook_enabled: boolean
  webhook_url: string
  webhook_secret: string
  webhook_timeout_seconds: number
  webhook_max_attempts: number
  smtp_enabled: boolean
  smtp_host: string
  smtp_port: number
  smtp_username: string
  smtp_password: string
  smtp_from: string
  smtp_to: string
  smtp_starttls: boolean
  smtp_timeout_seconds: number
  smtp_max_attempts: number
  levels: Array<'warning' | 'critical'>
  groups: string[]
  device_allow: string[]
  device_deny: string[]
  mute_minutes: number
  retry_backoff_seconds: number
}>

export type NotificationTestChannel = 'webhook' | 'smtp' | 'all'

export interface NotificationTestResult {
  channel: string
  status: 'sent' | 'failed'
  attempts: number
  error: string | null
}

/** v1.31.0：pending 为已落库、等待首投或重试的中间态 */
export type NotificationDeliveryStatus = 'pending' | 'sent' | 'failed' | 'suppressed'
export type NotificationDeliveryChannel = 'webhook' | 'smtp' | 'mute' | 'rule'

/** 投递记录：规则/分组快照 + 尝试次数 + 累计耗时 + 失败原因 */
export interface NotificationDelivery {
  id: number
  alert_id: string
  device_id: string
  metric: string
  level: string
  channel: string
  status: NotificationDeliveryStatus
  attempts: number
  error: string | null
  rule_id: number | null
  rule_name: string | null
  group_name: string | null
  duration_ms: number
  next_attempt_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string | null
}

/** 投递历史分页：total 由服务端统计，前端只按同一套筛选参数请求 */
export interface NotificationDeliveryPage {
  items: NotificationDelivery[]
  total: number
  page: number
  page_size: number
}

/** 通知规则：priority 越小越优先，同一告警只命中第一条匹配规则 */
export interface NotificationRule {
  id: number
  name: string
  enabled: boolean
  priority: number
  levels: string[]
  groups: string[]
  device_allow: string[]
  device_deny: string[]
  webhook_enabled: boolean
  webhook_url_configured: boolean
  webhook_secret_configured: boolean
  smtp_enabled: boolean
  smtp_to: string | null
  updated_at: string | null
  updated_by: string
}

export interface NotificationRulePayload {
  name: string
  enabled: boolean
  priority: number
  levels: Array<'warning' | 'critical'>
  groups: string[]
  device_allow: string[]
  device_deny: string[]
  webhook_enabled: boolean
  webhook_url?: string
  webhook_secret?: string
  smtp_enabled: boolean
  smtp_to?: string | null
}

/** 更新规则：敏感字段不传 = 保持原值，空串 = 清空 */
export type NotificationRulePatch = Partial<NotificationRulePayload>

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
