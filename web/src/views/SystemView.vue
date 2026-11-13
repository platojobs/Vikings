<script setup lang="ts">
/**
 * 系统状态：网关 / Broker / 存储 / 后台任务的只读运维快照（仅管理员）。
 *
 * 定位是排障入口：后台其他页面回答「业务上发生了什么」，
 * 这一页回答「故障在哪一层」——网关进程？MQTT 连接？数据库容量？
 * 每 10 秒自动刷新一次，不订阅 MQTT，Broker 断开时本页仍可用。
 *
 * v1.29.0：追加「告警通知通道」配置卡。
 * v1.31.0：通道传输设置与多规则分开——规则决定收件人与路由，
 * 传输设置只保留全局开关/超时/退避/SMTP 服务器；投递历史移至独立页面。
 * 服务端配置存库、每次派发热读取，因此保存后无需重启网关；
 * URL / 签名密钥 / SMTP 口令只显示「是否已配置」，表单留空表示保持原值。
 * v1.34.0：追加「告警规则」卡——阈值从告警中心抽屉迁到此处，支持指标/方向/
 * 阈值/级别/生效范围/连续次数的新增、编辑、启停（均二次确认）与删除，
 * 写入即时生效（网关判定读规则缓存）。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchSlaConfig, updateSlaConfig } from '@/api/alerts'
import { fetchDeviceGroups, fetchDeviceTags } from '@/api/devices'
import { fetchSystemStatus } from '@/api/system'
import {
  createNotificationRule,
  deleteNotificationRule,
  fetchNotificationConfig,
  fetchNotificationRules,
  sendTestNotification,
  updateNotificationConfig,
  updateNotificationRule,
} from '@/api/notifications'
import {
  createMaintenanceWindow,
  endMaintenanceWindow,
  fetchMaintenanceSummaries,
  fetchMaintenanceSuppressions,
  fetchMaintenanceWindows,
} from '@/api/maintenance'
import {
  createAlertRule,
  deleteAlertRule,
  fetchAlertRuleHistory,
  fetchAlertRules,
  restoreDefaultAlertRules,
  setAlertRuleEnabled,
  updateAlertRule,
} from '@/api/rules'
import { formatDateTime } from '@/utils/format'
import {
  cloneConditions,
  conditionFromRule,
  describeConditions,
  emptyConditions,
  validateConditions,
} from '@/utils/conditions'
import RuleConditionBuilder from '@/components/RuleConditionBuilder.vue'
import type {
  AlertMetric,
  AlertRule,
  AlertRuleConditionNode,
  AlertRuleCreateBody,
  AlertRuleHistoryEntry,
  MaintenanceSuppression,
  MaintenanceSummary,
  MaintenanceWindow,
  NotificationConfig,
  NotificationConfigPatch,
  NotificationRule,
  NotificationRulePatch,
  NotificationRulePayload,
  NotificationTestChannel,
  NotificationTestResult,
  SystemStatus,
} from '@/types'

const router = useRouter()

const status = ref<SystemStatus | null>(null)
const loading = ref(true)
const error = ref('')
let timer: ReturnType<typeof setInterval> | null = null

async function loadStatus(): Promise<void> {
  try {
    status.value = await fetchSystemStatus()
    error.value = ''
  } catch {
    error.value = '无法获取系统状态，请确认网关已启动且当前账号具备管理员权限'
  } finally {
    loading.value = false
  }
}

/** 运行时长拆成「天/时/分」，秒级细节对运维判断没有意义 */
function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days} 天 ${hours} 小时`
  if (hours > 0) return `${hours} 小时 ${minutes} 分`
  return `${minutes} 分钟`
}

const onlineRate = computed(() => {
  const storage = status.value?.storage
  if (!storage || storage.device_total === 0) return '—'
  return `${Math.round((storage.device_online / storage.device_total) * 100)}%`
})

const totalUptime = computed(() => formatUptime(status.value?.gateway.uptime_seconds ?? 0))

// ==================== 通知传输设置（v1.31.0） ====================

const notifyConfig = ref<NotificationConfig | null>(null)
const notifyLoading = ref(false)
const notifySaving = ref(false)
const clearSecretsBusy = ref(false)

const form = reactive({
  enabled: false,
  webhook_timeout_seconds: 10,
  webhook_max_attempts: 3,
  smtp_host: '',
  smtp_port: 587,
  smtp_username: '',
  smtp_password: '',
  smtp_from: '',
  smtp_starttls: true,
  smtp_timeout_seconds: 10,
  smtp_max_attempts: 3,
  mute_minutes: 10,
  retry_backoff_seconds: 1,
})

/** 逗号/顿号/换行都当作分隔符，避免运维从表格粘贴后配置不生效 */
function splitList(text: string): string[] {
  return text
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function applyConfig(config: NotificationConfig): void {
  notifyConfig.value = config
  form.enabled = config.enabled
  form.webhook_timeout_seconds = config.webhook_timeout_seconds
  form.webhook_max_attempts = config.webhook_max_attempts
  form.smtp_host = config.smtp_host ?? ''
  form.smtp_port = config.smtp_port
  form.smtp_username = config.smtp_username ?? ''
  form.smtp_from = config.smtp_from ?? ''
  form.smtp_starttls = config.smtp_starttls
  form.smtp_timeout_seconds = config.smtp_timeout_seconds
  form.smtp_max_attempts = config.smtp_max_attempts
  form.mute_minutes = config.mute_minutes
  form.retry_backoff_seconds = config.retry_backoff_seconds
  // 敏感字段永远不回显，保持空白（留空 = 保持原值）
  form.smtp_password = ''
}

async function loadNotificationConfig(): Promise<void> {
  notifyLoading.value = true
  try {
    applyConfig(await fetchNotificationConfig())
  } catch {
    // 请求层已提示；保留旧表单，避免误清空
  } finally {
    notifyLoading.value = false
  }
}

async function saveNotificationConfig(): Promise<void> {
  notifySaving.value = true
  try {
    const payload: NotificationConfigPatch = {
      enabled: form.enabled,
      webhook_timeout_seconds: form.webhook_timeout_seconds,
      webhook_max_attempts: form.webhook_max_attempts,
      smtp_host: form.smtp_host.trim(),
      smtp_port: form.smtp_port,
      smtp_username: form.smtp_username.trim(),
      smtp_from: form.smtp_from.trim(),
      smtp_starttls: form.smtp_starttls,
      smtp_timeout_seconds: form.smtp_timeout_seconds,
      smtp_max_attempts: form.smtp_max_attempts,
      mute_minutes: form.mute_minutes,
      retry_backoff_seconds: form.retry_backoff_seconds,
    }
    if (form.smtp_password.trim()) payload.smtp_password = form.smtp_password.trim()

    applyConfig(await updateNotificationConfig(payload))
    ElMessage.success('通知传输设置已保存，下一条告警即刻生效（无需重启）')
  } catch {
    // 请求层已提示
  } finally {
    notifySaving.value = false
  }
}

/** v1.29.0 遗留凭据只用于「没有任何规则」时的兼容回退，可一键清空 */
async function clearLegacySecrets(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将清空 v1.29.0 遗留的全局 Webhook URL / 签名密钥 / SMTP 口令。规则里的接收人不受影响，确认继续？',
      '清空遗留凭据',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  clearSecretsBusy.value = true
  try {
    applyConfig(
      await updateNotificationConfig({ webhook_url: '', webhook_secret: '', smtp_password: '' }),
    )
    ElMessage.success('遗留凭据已清空')
  } catch {
    // 请求层已提示
  } finally {
    clearSecretsBusy.value = false
  }
}

// ==================== 通知规则（v1.31.0） ====================

const rules = ref<NotificationRule[]>([])
const rulesLoading = ref(false)
const ruleDialogVisible = ref(false)
const ruleSaving = ref(false)
const editingRuleId = ref<number | null>(null)

const ruleForm = reactive({
  name: '',
  enabled: true,
  priority: 100,
  levels: ['critical'] as Array<'warning' | 'critical'>,
  groupsText: '',
  allowText: '',
  denyText: '',
  webhook_enabled: false,
  webhook_url: '',
  webhook_secret: '',
  smtp_enabled: false,
  smtp_to: '',
})

const editingRule = computed(
  () => rules.value.find((item) => item.id === editingRuleId.value) ?? null,
)

async function loadRules(): Promise<void> {
  rulesLoading.value = true
  try {
    rules.value = await fetchNotificationRules()
  } catch {
    rules.value = []
  } finally {
    rulesLoading.value = false
  }
}

function openCreateRule(): void {
  editingRuleId.value = null
  ruleForm.name = ''
  ruleForm.enabled = true
  ruleForm.priority = 100
  ruleForm.levels = ['critical']
  ruleForm.groupsText = ''
  ruleForm.allowText = ''
  ruleForm.denyText = ''
  ruleForm.webhook_enabled = false
  ruleForm.webhook_url = ''
  ruleForm.webhook_secret = ''
  ruleForm.smtp_enabled = false
  ruleForm.smtp_to = ''
  ruleDialogVisible.value = true
}

/** 编辑时 URL/密钥留空 = 保持原值，避免把敏感值回显到表单 */
function openEditRule(rule: NotificationRule): void {
  editingRuleId.value = rule.id
  ruleForm.name = rule.name
  ruleForm.enabled = rule.enabled
  ruleForm.priority = rule.priority
  ruleForm.levels = rule.levels.filter(
    (level): level is 'warning' | 'critical' => level === 'warning' || level === 'critical',
  )
  if (ruleForm.levels.length === 0) ruleForm.levels = ['critical']
  ruleForm.groupsText = rule.groups.join(', ')
  ruleForm.allowText = rule.device_allow.join(', ')
  ruleForm.denyText = rule.device_deny.join(', ')
  ruleForm.webhook_enabled = rule.webhook_enabled
  ruleForm.webhook_url = ''
  ruleForm.webhook_secret = ''
  ruleForm.smtp_enabled = rule.smtp_enabled
  ruleForm.smtp_to = rule.smtp_to ?? ''
  ruleDialogVisible.value = true
}

async function saveRule(): Promise<void> {
  if (!ruleForm.name.trim()) {
    ElMessage.warning('请填写规则名称')
    return
  }
  if (!ruleForm.webhook_enabled && !ruleForm.smtp_enabled) {
    ElMessage.warning('规则至少需要启用一个通道')
    return
  }
  if (
    ruleForm.webhook_enabled &&
    !ruleForm.webhook_url.trim() &&
    !editingRule.value?.webhook_url_configured
  ) {
    ElMessage.warning('启用 Webhook 时必须填写 URL')
    return
  }
  if (ruleForm.smtp_enabled && !ruleForm.smtp_to.trim()) {
    ElMessage.warning('启用 SMTP 时必须填写收件人')
    return
  }

  const payload: NotificationRulePayload = {
    name: ruleForm.name.trim(),
    enabled: ruleForm.enabled,
    priority: ruleForm.priority,
    levels: ruleForm.levels,
    groups: splitList(ruleForm.groupsText),
    device_allow: splitList(ruleForm.allowText),
    device_deny: splitList(ruleForm.denyText),
    webhook_enabled: ruleForm.webhook_enabled,
    smtp_enabled: ruleForm.smtp_enabled,
    smtp_to: ruleForm.smtp_to.trim(),
  }
  if (ruleForm.webhook_url.trim()) payload.webhook_url = ruleForm.webhook_url.trim()
  if (ruleForm.webhook_secret.trim()) payload.webhook_secret = ruleForm.webhook_secret.trim()

  ruleSaving.value = true
  try {
    if (editingRuleId.value !== null) {
      await updateNotificationRule(editingRuleId.value, payload as NotificationRulePatch)
      ElMessage.success('规则已更新，下一批告警按新优先级与分组路由')
    } else {
      await createNotificationRule(payload)
      ElMessage.success('规则已创建，下一条告警即刻生效')
    }
    ruleDialogVisible.value = false
    await loadRules()
  } catch {
    // 请求层已提示
  } finally {
    ruleSaving.value = false
  }
}

async function removeRule(rule: NotificationRule): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除规则「${rule.name}」？之后匹配不到其他规则的告警将记为 suppressed（不投递）。`,
      '删除通知规则',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteNotificationRule(rule.id)
    ElMessage.success('规则已删除')
    await loadRules()
  } catch {
    // 请求层已提示
  }
}

// ==================== 测试通知 ====================

const testing = ref(false)
const testChannel = ref<NotificationTestChannel>('all')
const testRuleId = ref<number | ''>('')
const testResults = ref<NotificationTestResult[]>([])

async function runTestNotification(): Promise<void> {
  testing.value = true
  testResults.value = []
  try {
    testResults.value = await sendTestNotification(
      testChannel.value,
      testRuleId.value === '' ? undefined : Number(testRuleId.value),
    )
    const failed = testResults.value.filter((item) => item.status !== 'sent')
    if (failed.length === 0) {
      ElMessage.success('测试通知已真实发送，请到接收端确认')
    } else {
      ElMessage.warning(`${failed.length} 个通道发送失败，请查看下方原因`)
    }
  } catch {
    // 请求层已提示
  } finally {
    testing.value = false
  }
}

// ==================== 告警 SLA 配置（v1.33.0） ====================

const slaLoading = ref(false)
const slaSaving = ref(false)
const slaUpdated = ref('')
const slaForm = reactive({
  critical_minutes: 30,
  warning_minutes: 120,
  max_escalations: 2,
  escalation_interval_minutes: 30,
})

async function loadSlaConfig(): Promise<void> {
  slaLoading.value = true
  try {
    const config = await fetchSlaConfig()
    slaForm.critical_minutes = config.critical_minutes
    slaForm.warning_minutes = config.warning_minutes
    slaForm.max_escalations = config.max_escalations
    slaForm.escalation_interval_minutes = config.escalation_interval_minutes
    slaUpdated.value = config.updated_at
      ? `${config.updated_by} · ${formatDateTime(config.updated_at)}`
      : ''
  } catch {
    // 请求层已提示
  } finally {
    slaLoading.value = false
  }
}

/** 修改只影响之后创建的告警：历史告警保留创建时的时限快照，可复盘 */
async function saveSlaConfig(): Promise<void> {
  slaSaving.value = true
  try {
    const config = await updateSlaConfig({ ...slaForm })
    slaForm.critical_minutes = config.critical_minutes
    slaForm.warning_minutes = config.warning_minutes
    slaForm.max_escalations = config.max_escalations
    slaForm.escalation_interval_minutes = config.escalation_interval_minutes
    slaUpdated.value = config.updated_at
      ? `${config.updated_by} · ${formatDateTime(config.updated_at)}`
      : ''
    ElMessage.success('SLA 配置已保存，新告警立即生效')
  } catch {
    // 请求层已提示
  } finally {
    slaSaving.value = false
  }
}

// ==================== 告警规则（v1.34.0） ====================

const alertRules = ref<AlertRule[]>([])
const alertRulesLoading = ref(false)
const ruleEditorVisible = ref(false)
const ruleEditorSaving = ref(false)
const editingAlertRuleId = ref<number | null>(null)

/** 指标元数据：前端只做输入范围约束，最终以服务端校验为准 */
const METRIC_OPTIONS: Array<{
  value: AlertMetric
  label: string
  unit: string
  min: number
  max: number
  step: number
  ops: Array<'lt' | 'gt'>
}> = [
  { value: 'voltage', label: '电压', unit: 'V', min: 2, max: 4.5, step: 0.1, ops: ['lt'] },
  { value: 'temperature', label: '温度', unit: '℃', min: -40, max: 85, step: 1, ops: ['lt', 'gt'] },
  { value: 'humidity', label: '湿度', unit: '%', min: 0, max: 100, step: 1, ops: ['gt'] },
  { value: 'rssi', label: '信号强度', unit: 'dBm', min: -150, max: 0, step: 1, ops: ['lt'] },
]

const alertRuleForm = reactive({
  name: '',
  metric: 'voltage' as AlertMetric,
  op: 'lt' as 'lt' | 'gt',
  threshold: 3.4,
  level: 'warning' as 'warning' | 'critical',
  /** v1.38.0：global=无范围；conditions=条件树（且/或嵌套） */
  scope_mode: 'global' as 'global' | 'conditions',
  conditions: null as AlertRuleConditionNode | null,
  enabled: true,
  consecutive_count: 1,
  priority: 100,
})

// 条件编辑器的候选值：分组与标签来自真实数据，设备 ID 允许手输（设备可能很多）
const ruleGroupOptions = ref<string[]>([])
const ruleTagOptions = ref<string[]>([])

async function loadRuleScopeOptions(): Promise<void> {
  const [groups, tags] = await Promise.allSettled([fetchDeviceGroups(), fetchDeviceTags()])
  if (groups.status === 'fulfilled') {
    ruleGroupOptions.value = groups.value.map((item) => item.group_name).filter(Boolean)
  }
  if (tags.status === 'fulfilled') {
    ruleTagOptions.value = tags.value.map((item) => item.tag).filter(Boolean)
  }
}

const currentMetric = computed(
  () => METRIC_OPTIONS.find((item) => item.value === alertRuleForm.metric) ?? METRIC_OPTIONS[0],
)

function metricMeta(metric: AlertMetric) {
  return METRIC_OPTIONS.find((item) => item.value === metric) ?? METRIC_OPTIONS[0]
}

function opLabel(op: 'lt' | 'gt'): string {
  return op === 'gt' ? '高于' : '低于'
}

function ruleConditionText(rule: AlertRule): string {
  const meta = metricMeta(rule.metric)
  return `${opLabel(rule.op)} ${rule.threshold}${meta.unit}`
}

function ruleScopeValues(rule: AlertRule): string[] {
  if (rule.scope_values?.length) return rule.scope_values
  return rule.scope_value ? [rule.scope_value] : []
}

function scopeText(rule: AlertRule): string {
  if (rule.conditions) return describeConditions(rule.conditions)
  if (rule.scope_type === 'global') return '全局'
  if (rule.scope_type === 'custom') return '条件树（未配置）'
  const values = ruleScopeValues(rule)
  const label =
    rule.scope_type === 'group' ? '分组' : rule.scope_type === 'device' ? '设备' : '标签'
  return `${label}：${values.length ? values.join('、') : '（未配置）'}`
}

/** 切换范围模式：进入条件编辑时给出默认骨架，返回全局时清空条件树 */
function onScopeModeChange(mode: 'global' | 'conditions'): void {
  alertRuleForm.scope_mode = mode
  if (mode === 'global') {
    alertRuleForm.conditions = null
  } else {
    alertRuleForm.conditions = alertRuleForm.conditions ?? emptyConditions()
  }
}

/** 换指标时同步比较方向与阈值范围，避免把电压阈值带到湿度规则上 */
function onMetricChange(): void {
  const meta = currentMetric.value
  if (!meta.ops.includes(alertRuleForm.op)) alertRuleForm.op = meta.ops[0]
  alertRuleForm.threshold = Math.min(Math.max(alertRuleForm.threshold, meta.min), meta.max)
}

async function loadAlertRules(): Promise<void> {
  alertRulesLoading.value = true
  try {
    alertRules.value = await fetchAlertRules()
  } catch {
    alertRules.value = []
  } finally {
    alertRulesLoading.value = false
  }
}

function openCreateAlertRule(): void {
  editingAlertRuleId.value = null
  alertRuleForm.name = ''
  alertRuleForm.metric = 'voltage'
  alertRuleForm.op = 'lt'
  alertRuleForm.threshold = 3.4
  alertRuleForm.level = 'warning'
  alertRuleForm.scope_mode = 'global'
  alertRuleForm.conditions = null
  alertRuleForm.enabled = true
  alertRuleForm.consecutive_count = 1
  alertRuleForm.priority = 100
  ruleEditorVisible.value = true
  void loadRuleScopeOptions()
}

function openEditAlertRule(rule: AlertRule): void {
  editingAlertRuleId.value = rule.id
  alertRuleForm.name = rule.name
  alertRuleForm.metric = rule.metric
  alertRuleForm.op = rule.op
  alertRuleForm.threshold = rule.threshold
  alertRuleForm.level = rule.level
  const equivalent = conditionFromRule(rule)
  alertRuleForm.scope_mode = equivalent ? 'conditions' : 'global'
  alertRuleForm.conditions = equivalent
  alertRuleForm.enabled = rule.enabled
  alertRuleForm.consecutive_count = rule.consecutive_count
  alertRuleForm.priority = rule.priority
  ruleEditorVisible.value = true
  void loadRuleScopeOptions()
}

async function saveAlertRule(): Promise<void> {
  if (!alertRuleForm.name.trim()) {
    ElMessage.warning('请填写规则名称')
    return
  }

  let payload: AlertRuleCreateBody
  if (alertRuleForm.scope_mode === 'conditions' && alertRuleForm.conditions) {
    const conditions = cloneConditions(alertRuleForm.conditions)
    const invalid = validateConditions(conditions)
    if (invalid) {
      ElMessage.warning(invalid)
      return
    }
    // 条件树是权威范围：scope_type=custom，老字段置空；单叶子由服务端镜像回老字段，
    // 便于 downgrade 到 v1.35 后仍能读取等价范围。
    payload = {
      name: alertRuleForm.name.trim(),
      metric: alertRuleForm.metric,
      op: alertRuleForm.op,
      threshold: alertRuleForm.threshold,
      level: alertRuleForm.level,
      scope_type: 'custom',
      scope_value: null,
      scope_values: null,
      conditions,
      enabled: alertRuleForm.enabled,
      consecutive_count: alertRuleForm.consecutive_count,
      priority: alertRuleForm.priority,
    }
  } else {
    payload = {
      name: alertRuleForm.name.trim(),
      metric: alertRuleForm.metric,
      op: alertRuleForm.op,
      threshold: alertRuleForm.threshold,
      level: alertRuleForm.level,
      scope_type: 'global',
      scope_value: null,
      scope_values: null,
      conditions: null,
      enabled: alertRuleForm.enabled,
      consecutive_count: alertRuleForm.consecutive_count,
      priority: alertRuleForm.priority,
    }
  }

  ruleEditorSaving.value = true
  try {
    if (editingAlertRuleId.value !== null) {
      await updateAlertRule(editingAlertRuleId.value, payload)
      ElMessage.success('规则已更新，下一次上报立即按新阈值判定')
    } else {
      await createAlertRule(payload)
      ElMessage.success('规则已创建并即时生效')
    }
    ruleEditorVisible.value = false
    await loadAlertRules()
  } catch {
    // 请求层已提示（含服务端 400 的可读校验信息）
  } finally {
    ruleEditorSaving.value = false
  }
}

async function toggleAlertRule(rule: AlertRule): Promise<void> {
  const action = rule.enabled ? '停用' : '启用'
  try {
    await ElMessageBox.confirm(
      `${action}规则「${rule.name}」？${rule.enabled ? '停用后该规则不再产生新告警，历史告警与统计不受影响。' : '启用后下一次上报立即参与判定。'}`,
      `${action}告警规则`,
      { type: 'warning', confirmButtonText: action, cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await setAlertRuleEnabled(rule.id, !rule.enabled)
    ElMessage.success(`规则已${action}`)
    await loadAlertRules()
  } catch {
    // 请求层已提示
  }
}

async function removeAlertRule(rule: AlertRule): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除规则「${rule.name}」？删除后不可恢复；需要保留判定依据时请优先选择「停用」。`,
      '删除告警规则',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteAlertRule(rule.id)
    ElMessage.success('规则已删除')
    await loadAlertRules()
  } catch {
    // 请求层已提示
  }
}

async function restoreAlertRules(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '把所有默认规则的阈值恢复为环境变量/出厂值，自定义规则保持不变。历史告警不受影响，确认继续？',
      '恢复默认阈值',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await restoreDefaultAlertRules()
    ElMessage.success(`已恢复 ${result.restored} 条默认规则阈值`)
    await loadAlertRules()
  } catch {
    // 请求层已提示
  }
}

// ==================== 规则变更历史（v1.35.0） ====================

/** 历史直接读既有 audit_log（target_type=alert_rule），不另建平行记录 */
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyRule = ref<AlertRule | null>(null)
const historyEntries = ref<AlertRuleHistoryEntry[]>([])

async function openAlertRuleHistory(rule: AlertRule): Promise<void> {
  historyRule.value = rule
  historyEntries.value = []
  historyVisible.value = true
  historyLoading.value = true
  try {
    historyEntries.value = await fetchAlertRuleHistory(rule.id)
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    historyLoading.value = false
  }
}

const CHANGE_FIELD_LABELS: Record<string, string> = {
  name: '名称',
  metric: '指标',
  op: '比较方向',
  threshold: '阈值',
  level: '级别',
  enabled: '启用状态',
  consecutive_count: '连续次数',
  priority: '优先级',
  scope_type: '范围类型',
  scope_value: '范围值',
  scope_values: '范围值',
  conditions: '条件树',
  deleted: '删除快照',
  restored_from: '恢复前阈值',
  restored_to: '恢复后阈值',
}

function looksLikeConditions(value: unknown): value is AlertRuleConditionNode {
  return (
    typeof value === 'object' &&
    value !== null &&
    'op' in value &&
    'children' in value &&
    Array.isArray((value as { children?: unknown }).children)
  )
}

/** 数值统一转文本；数组用顿号拼接，布尔按启用/停用展示，条件树转可读描述 */
function formatChangeValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '空'
  if (Array.isArray(value)) return value.length ? value.join('、') : '空'
  if (typeof value === 'boolean') return value ? '启用' : '停用'
  if (looksLikeConditions(value)) return describeConditions(value)
  if (typeof value === 'object') {
    const truncated = (value as { truncated?: boolean }).truncated
    if (truncated) {
      const chars = (value as { chars?: number }).chars
      return `内容过长已截断（${chars ?? '?'} 字符）`
    }
    return JSON.stringify(value)
  }
  return String(value)
}

function changeRows(entry: AlertRuleHistoryEntry): Array<{
  field: string
  from: string
  to: string
}> {
  return Object.entries(entry.changes ?? {}).map(([field, change]) => ({
    field: CHANGE_FIELD_LABELS[field] ?? field,
    from: formatChangeValue(change?.from),
    to: formatChangeValue(change?.to),
  }))
}

// v1.39.0：维护窗口——窗口内命中规则的上报只记抑制记录，不入告警/通知/SLA
const maintenanceWindows = ref<MaintenanceWindow[]>([])
const maintenanceLoading = ref(false)
const mwDialogVisible = ref(false)
const mwSaving = ref(false)
const mwForm = reactive({
  name: '',
  reason: '',
  mode: 'once' as 'once' | 'weekly',
  starts_at: '',
  ends_at: '',
  weekday: 6,
  start: '18:00',
  duration_minutes: 120,
  scope_mode: 'global' as 'global' | 'conditions',
  conditions: null as AlertRuleConditionNode | null,
})

const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

const mwSummaryVisible = ref(false)
const mwSummaryWindow = ref<MaintenanceWindow | null>(null)
const mwSummaries = ref<MaintenanceSummary[]>([])
const mwSuppressionVisible = ref(false)
const mwSuppressionWindow = ref<MaintenanceWindow | null>(null)
const mwSuppressions = ref<MaintenanceSuppression[]>([])

async function loadMaintenanceWindows(): Promise<void> {
  maintenanceLoading.value = true
  try {
    maintenanceWindows.value = await fetchMaintenanceWindows()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '维护窗口加载失败')
  } finally {
    maintenanceLoading.value = false
  }
}

function openCreateMaintenance(): void {
  mwForm.name = ''
  mwForm.reason = ''
  mwForm.mode = 'once'
  mwForm.starts_at = ''
  mwForm.ends_at = ''
  mwForm.weekday = 6
  mwForm.start = '18:00'
  mwForm.duration_minutes = 120
  mwForm.scope_mode = 'global'
  mwForm.conditions = emptyConditions()
  mwDialogVisible.value = true
  void loadRuleScopeOptions()
}

function mwScopeText(row: MaintenanceWindow): string {
  return row.scope ? describeConditions(row.scope) : '全部设备'
}

function mwTimeText(row: MaintenanceWindow): string {
  if (row.mode === 'weekly') {
    const rec = row.recurrence
    if (!rec) return '—'
    return `每周${WEEKDAY_LABELS[rec.weekday] ?? rec.weekday} ${rec.start}（UTC）· ${rec.duration_minutes} 分钟`
  }
  const start = row.starts_at ? formatDateTime(row.starts_at) : '—'
  const end = row.ends_at ? formatDateTime(row.ends_at) : '—'
  return `${start} → ${end}`
}

function mwStatusText(row: MaintenanceWindow): string {
  if (row.status === 'ended') return '已结束'
  return row.active ? '进行中' : '未开始'
}

async function submitMaintenance(): Promise<void> {
  if (!mwForm.name.trim()) {
    ElMessage.warning('请填写窗口名称')
    return
  }
  const body: Parameters<typeof createMaintenanceWindow>[0] = {
    name: mwForm.name.trim(),
    reason: mwForm.reason.trim() || null,
    mode: mwForm.mode,
    scope: mwForm.scope_mode === 'conditions' ? mwForm.conditions : null,
  }
  if (mwForm.mode === 'once') {
    if (!mwForm.starts_at || !mwForm.ends_at) {
      ElMessage.warning('请选择开始与结束时间')
      return
    }
    body.starts_at = new Date(mwForm.starts_at).toISOString()
    body.ends_at = new Date(mwForm.ends_at).toISOString()
  } else {
    body.recurrence = {
      weekday: mwForm.weekday,
      start: mwForm.start,
      duration_minutes: mwForm.duration_minutes,
    }
  }
  if (mwForm.scope_mode === 'conditions') {
    const problem = mwForm.conditions ? validateConditions(mwForm.conditions) : '请配置条件树'
    if (problem) {
      ElMessage.warning(problem)
      return
    }
  }
  mwSaving.value = true
  try {
    await createMaintenanceWindow(body)
    ElMessage.success('维护窗口已创建')
    mwDialogVisible.value = false
    await loadMaintenanceWindows()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '创建失败')
  } finally {
    mwSaving.value = false
  }
}

async function confirmEndMaintenance(row: MaintenanceWindow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '结束后立即恢复正常告警，已抑制的历史记录保留且不会回填成告警。确认结束？',
      '提前结束维护窗口',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await endMaintenanceWindow(row.window_id)
    ElMessage.success('窗口已结束，已生成汇总')
    await loadMaintenanceWindows()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '结束失败')
  }
}

async function openSummaries(row: MaintenanceWindow): Promise<void> {
  mwSummaryWindow.value = row
  mwSummaries.value = []
  mwSummaryVisible.value = true
  try {
    mwSummaries.value = await fetchMaintenanceSummaries(row.window_id)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '汇总加载失败')
  }
}

async function openSuppressions(row: MaintenanceWindow): Promise<void> {
  mwSuppressionWindow.value = row
  mwSuppressions.value = []
  mwSuppressionVisible.value = true
  try {
    const page = await fetchMaintenanceSuppressions(row.window_id, 1, 50)
    mwSuppressions.value = page.items
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '抑制记录加载失败')
  }
}

onMounted(() => {
  loadStatus()
  loadNotificationConfig()
  loadRules()
  loadSlaConfig()
  loadAlertRules()
  loadMaintenanceWindows()
  void loadRuleScopeOptions()
  timer = setInterval(loadStatus, 10_000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div v-loading="loading" class="page system-page">
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
    />

    <el-alert
      v-if="status && !status.mqtt.connected"
      type="warning"
      show-icon
      :closable="false"
      title="MQTT Broker 未连接"
    >
      <template #default>
        设备上报与指令下发均不可用，最近错误：{{ status.mqtt.last_error || '—' }}
      </template>
    </el-alert>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">运行概览</span>
          <span class="section-desc">
            数据生成于 {{ formatDateTime(status.generated_at) }}，每 10 秒自动刷新
          </span>
        </div>
        <el-button size="small" @click="loadStatus">立即刷新</el-button>
      </div>

      <el-row :gutter="14" class="metric-row">
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">网关运行时长</div>
            <div class="metric-value">{{ totalUptime }}</div>
            <div class="metric-extra">
              版本 {{ status.gateway.version }} · 启动于
              {{ formatDateTime(status.gateway.started_at) }}
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">MQTT Broker</div>
            <div class="metric-value" :class="status.mqtt.connected ? 'ok' : 'bad'">
              {{ status.mqtt.connected ? '已连接' : '未连接' }}
            </div>
            <div class="metric-extra">
              {{ status.mqtt.host }}:{{ status.mqtt.port }} · 已消费
              {{ status.mqtt.messages_handled }} 条
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">设备在线率</div>
            <div class="metric-value">{{ onlineRate }}</div>
            <div class="metric-extra">
              在线 {{ status.storage.device_online }} / 总数
              {{ status.storage.device_total }}
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">时序记录</div>
            <div class="metric-value">{{ status.storage.record_total }}</div>
            <div class="metric-extra">
              未确认告警 {{ status.storage.alert_open }} / 累计
              {{ status.storage.alert_total }}
            </div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <span class="section-title">存储与容量</span>
      </div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="数据库">
          {{ status.storage.backend === 'sqlite' ? 'SQLite（本地演示）' : 'PostgreSQL' }}
        </el-descriptions-item>
        <el-descriptions-item label="设备状态分布">
          在线 {{ status.storage.device_online }} · 离线
          {{ status.storage.device_offline }}
        </el-descriptions-item>
        <el-descriptions-item label="最早一条上报">
          {{ formatDateTime(status.storage.record_oldest_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="最新一条上报">
          {{ formatDateTime(status.storage.record_newest_at) }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <span class="section-title">后台任务与业务参数</span>
        <span class="section-desc">只读展示，修改需调整网关环境变量后重启</span>
      </div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="离线判定阈值">
          {{ status.scheduler.offline_timeout_seconds }} 秒未上报即置离线
        </el-descriptions-item>
        <el-descriptions-item label="离线巡检周期">
          每 {{ status.scheduler.offline_sweep_interval_seconds }} 秒执行一次
        </el-descriptions-item>
        <el-descriptions-item label="时序数据保留">
          {{ status.scheduler.data_retention_days }} 天
        </el-descriptions-item>
        <el-descriptions-item label="数据清理周期">
          每 {{ status.scheduler.data_purge_interval_seconds }} 秒执行一次
        </el-descriptions-item>
        <el-descriptions-item label="告警入库抑制窗口">
          同设备同指标 {{ status.scheduler.alert_suppress_seconds }} 秒内只入库一次
        </el-descriptions-item>
        <el-descriptions-item label="通知静默窗口">
          下方「通知规则」按分组配置接收人，抑制只影响发送，不影响告警入库
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card v-loading="slaLoading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">告警 SLA 与超时升级</span>
          <span class="section-desc">
            按级别配置响应时限与升级上限；修改只影响新告警，历史告警保留创建时的时限快照
            <template v-if="slaUpdated">（最近修改：{{ slaUpdated }}）</template>
          </span>
        </div>
        <div class="head-actions">
          <el-button size="small" type="primary" :loading="slaSaving" @click="saveSlaConfig">
            保存配置
          </el-button>
        </div>
      </div>
      <el-form label-width="150px" class="sla-form">
        <el-form-item label="严重告警响应时限">
          <el-input-number
            v-model="slaForm.critical_minutes"
            :min="1"
            :max="10080"
            controls-position="right"
          />
          <span class="form-hint">分钟；从告警创建到确认</span>
        </el-form-item>
        <el-form-item label="警告告警响应时限">
          <el-input-number
            v-model="slaForm.warning_minutes"
            :min="1"
            :max="10080"
            controls-position="right"
          />
          <span class="form-hint">分钟；默认比严重告警更长</span>
        </el-form-item>
        <el-form-item label="升级上限">
          <el-input-number
            v-model="slaForm.max_escalations"
            :min="0"
            :max="10"
            controls-position="right"
          />
          <span class="form-hint">0 表示不升级；达上限后停止升级，但告警仍标记「已超时」</span>
        </el-form-item>
        <el-form-item label="升级间隔">
          <el-input-number
            v-model="slaForm.escalation_interval_minutes"
            :min="1"
            :max="10080"
            controls-position="right"
          />
          <span class="form-hint">分钟；第 N 次升级 = 截止时间 + (N-1) × 间隔</span>
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="升级通知复用现有通知规则链路，内容包含「超时升级、第几次、原 SLA 时长」；同一条告警同一步只发一次。"
      />
    </el-card>

    <el-card v-loading="alertRulesLoading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">告警规则</span>
          <span class="section-desc">
            按「指标 + 比较方向 + 阈值 + 级别 + 生效范围」判定；范围可填多个分组/设备/标签，
            同一条规则内多个值之间为「或」；同一指标多条命中取最高级别（critical 优先），
            同级按优先级数字小的优先，每指标只产一条告警；保存后下一次上报即时生效
          </span>
        </div>
        <div class="head-actions">
          <el-button size="small" @click="restoreAlertRules">恢复默认阈值</el-button>
          <el-button size="small" type="primary" @click="openCreateAlertRule">新增规则</el-button>
        </div>
      </div>

      <el-alert type="warning" :closable="false" show-icon class="notify-hint">
        <template #title>
          停用规则只停止产生新告警，历史告警与统计不受影响；删除规则不可恢复。
          阈值方向与级别关系由服务端校验（严重阈值必须比警告更严格），误配会被拒绝并给出原因。
        </template>
      </el-alert>

      <el-table :data="alertRules" size="small" empty-text="暂无规则：所有指标都不会产生告警">
        <el-table-column label="规则名称" min-width="170">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.is_default" size="small" type="info" class="inline-tag">默认</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="指标" width="110">
          <template #default="{ row }">
            {{ metricMeta(row.metric).label }}（{{ metricMeta(row.metric).unit }}）
          </template>
        </el-table-column>
        <el-table-column label="条件" width="130">
          <template #default="{ row }">{{ ruleConditionText(row) }}</template>
        </el-table-column>
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="row.level === 'critical' ? 'danger' : 'warning'" size="small">
              {{ row.level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="生效范围" min-width="150">
          <template #default="{ row }">{{ scopeText(row) }}</template>
        </el-table-column>
        <el-table-column label="连续次数" width="100">
          <template #default="{ row }">
            {{ row.consecutive_count > 1 ? `连续 ${row.consecutive_count} 次` : '首次越限' }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近修改" min-width="190">
          <template #default="{ row }">
            {{ row.updated_by }} · {{ row.updated_at ? formatDateTime(row.updated_at) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link size="small" @click="openEditAlertRule(row)">编辑</el-button>
            <el-button link size="small" @click="toggleAlertRule(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button link size="small" @click="openAlertRuleHistory(row)">历史</el-button>
            <el-button link size="small" type="danger" @click="removeAlertRule(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-loading="maintenanceLoading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">计划维护窗口</span>
          <span class="section-desc">
            窗口内命中规则的设备上报只记「抑制记录」，不产生告警、不发通知、不占 SLA；
            窗口结束后生成汇总，critical 被压掉的条数会单独标出。时间一律按 UTC 计算。
          </span>
        </div>
        <div class="head-actions">
          <el-button size="small" @click="loadMaintenanceWindows">刷新</el-button>
          <el-button size="small" type="primary" @click="openCreateMaintenance">
            新增窗口
          </el-button>
        </div>
      </div>

      <el-table :data="maintenanceWindows" size="small" empty-text="暂无维护窗口">
        <el-table-column label="名称" min-width="150">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <div class="form-hint">{{ row.reason || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="范围" min-width="170">
          <template #default="{ row }">{{ mwScopeText(row) }}</template>
        </el-table-column>
        <el-table-column label="时间" min-width="220">
          <template #default="{ row }">
            {{ mwTimeText(row) }}
            <div v-if="row.next_start" class="form-hint">
              下次：{{ formatDateTime(row.next_start) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag
              :type="row.active ? 'danger' : row.status === 'ended' ? 'info' : 'success'"
              size="small"
            >
              {{ mwStatusText(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建" min-width="150">
          <template #default="{ row }">
            {{ row.created_by }} · {{ formatDateTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link size="small" @click="openSummaries(row)">汇总</el-button>
            <el-button link size="small" @click="openSuppressions(row)">抑制记录</el-button>
            <el-button
              link
              size="small"
              type="danger"
              :disabled="row.status === 'ended'"
              @click="confirmEndMaintenance(row)"
            >
              提前结束
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-loading="notifyLoading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">告警通知：传输设置</span>
          <span class="section-desc">
            全局开关 / 超时 / 退避 / SMTP 服务器存库并热读取；接收人在下方「通知规则」中配置
          </span>
        </div>
        <div class="head-actions">
          <el-button size="small" :disabled="clearSecretsBusy" @click="clearLegacySecrets">
            清空遗留凭据
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="notifySaving"
            @click="saveNotificationConfig"
          >
            保存配置
          </el-button>
        </div>
      </div>

      <el-alert type="info" :closable="false" show-icon class="notify-hint">
        <template #title>
          通知是旁路异步投递：发送失败不影响告警入库与 MQTT 链路；静默窗口内只不发通知，
          告警仍会进入告警中心与移动端 App。待重试任务落库，网关重启后会继续投递。
        </template>
      </el-alert>

      <el-form label-width="130px" label-position="left" class="notify-form">
        <el-divider content-position="left">总开关与去重</el-divider>
        <el-form-item label="启用通知">
          <el-switch v-model="form.enabled" />
          <span class="form-hint">关闭后所有告警只入库、不发送任何通知</span>
        </el-form-item>
        <el-form-item label="静默窗口">
          <el-input-number v-model="form.mute_minutes" :min="0" :max="1440" />
          <span class="form-hint">
            分钟；同设备同指标窗口内只通知一次，0 表示不去重（与 v1.29.0 语义一致）
          </span>
        </el-form-item>
        <el-form-item label="重试退避">
          <el-input-number v-model="form.retry_backoff_seconds" :min="0" :max="60" :step="0.5" />
          <span class="form-hint">秒；指数退避 = 基础值 × 2^(第几次-1)，用尽后终态 failed</span>
        </el-form-item>

        <el-divider content-position="left">Webhook 传输</el-divider>
        <el-form-item label="超时 / 重试">
          <el-input-number v-model="form.webhook_timeout_seconds" :min="1" :max="60" />
          <span class="inline-text">秒，最多重试</span>
          <el-input-number v-model="form.webhook_max_attempts" :min="1" :max="10" />
          <span class="inline-text">次</span>
        </el-form-item>
        <el-form-item label="接收端">
          <span class="form-hint">
            URL 与签名密钥按规则独立配置（见下方规则表）；升级时 v1.29.0 的全局 Webhook
            已自动迁移为「默认规则」
          </span>
        </el-form-item>

        <el-divider content-position="left">SMTP 传输</el-divider>
        <el-form-item label="服务器 / 端口">
          <el-input v-model="form.smtp_host" placeholder="smtp.example.com" class="host-input" />
          <el-input-number v-model="form.smtp_port" :min="1" :max="65535" :controls="false" class="port-input" />
          <el-switch v-model="form.smtp_starttls" active-text="STARTTLS" />
        </el-form-item>
        <el-form-item label="账号 / 口令">
          <el-input v-model="form.smtp_username" placeholder="登录账号（可留空）" class="half-input" autocomplete="off" />
          <el-input
            v-model="form.smtp_password"
            type="password"
            placeholder="口令（已配置时留空保持不变）"
            autocomplete="new-password"
            show-password
            class="half-input"
          />
        </el-form-item>
        <el-form-item label="发件人">
          <el-input v-model="form.smtp_from" placeholder="alerts@example.com" class="half-input" />
          <span class="form-hint">收件人按规则配置，多个用逗号分隔</span>
        </el-form-item>
        <el-form-item label="超时 / 重试">
          <el-input-number v-model="form.smtp_timeout_seconds" :min="1" :max="60" />
          <span class="inline-text">秒，最多重试</span>
          <el-input-number v-model="form.smtp_max_attempts" :min="1" :max="10" />
          <span class="inline-text">次</span>
        </el-form-item>
      </el-form>

      <div class="test-panel">
        <div class="test-controls">
          <span class="test-label">测试通知</span>
          <el-radio-group v-model="testChannel" size="small">
            <el-radio-button value="all">全部通道</el-radio-button>
            <el-radio-button value="webhook">Webhook</el-radio-button>
            <el-radio-button value="smtp">SMTP</el-radio-button>
          </el-radio-group>
          <el-select v-model="testRuleId" size="small" placeholder="默认最高优先级规则" clearable style="width: 200px">
            <el-option v-for="rule in rules" :key="rule.id" :label="`${rule.name}（P${rule.priority}）`" :value="rule.id" />
          </el-select>
          <el-button size="small" type="primary" plain :loading="testing" @click="runTestNotification">
            发送测试通知
          </el-button>
          <span class="form-hint">真实调用通道，忽略规则匹配与静默；结果会写入投递记录</span>
        </div>
        <div v-if="testResults.length" class="test-results">
          <el-tag
            v-for="item in testResults"
            :key="item.channel"
            :type="item.status === 'sent' ? 'success' : 'danger'"
            size="small"
          >
            {{ item.channel }}：{{ item.status === 'sent' ? '已发送' : `失败（${item.error || '未知错误'}）` }}
            · 尝试 {{ item.attempts }} 次
          </el-tag>
        </div>
      </div>

      <div v-if="notifyConfig?.updated_at" class="updated-at">
        最近更新：{{ formatDateTime(notifyConfig.updated_at) }} · 操作人
        {{ notifyConfig.updated_by }}
      </div>
    </el-card>

    <el-card v-loading="rulesLoading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">通知规则</span>
          <span class="section-desc">
            按 priority 升序（数字越小越优先）、同优先级按创建顺序，取第一条命中的规则；
            命中后不再尝试后续规则
          </span>
        </div>
        <div class="head-actions">
          <el-button size="small" @click="router.push('/notifications')">查看投递历史</el-button>
          <el-button size="small" type="primary" @click="openCreateRule">新增规则</el-button>
        </div>
      </div>

      <el-alert type="warning" :closable="false" show-icon class="notify-hint">
        <template #title>
          未匹配任何规则的告警不会投递，记为 suppressed（原因：未匹配任何通知规则）；
          设备分组变更后下一条告警立即按新分组生效
        </template>
      </el-alert>

      <el-table :data="rules" size="small" empty-text="暂无规则：所有告警都不会投递">
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag size="small" type="info">P{{ row.priority }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="规则名称" min-width="150" />
        <el-table-column label="级别" width="130">
          <template #default="{ row }">{{ row.levels.join(' / ') }}</template>
        </el-table-column>
        <el-table-column label="分组范围" min-width="120">
          <template #default="{ row }">{{ row.groups.length ? row.groups.join(', ') : '全部分组' }}</template>
        </el-table-column>
        <el-table-column label="设备范围" min-width="150">
          <template #default="{ row }">
            <span v-if="row.device_allow.length || row.device_deny.length">
              白名单 {{ row.device_allow.length }} / 黑名单 {{ row.device_deny.length }}
            </span>
            <span v-else class="muted">不限</span>
          </template>
        </el-table-column>
        <el-table-column label="接收人" min-width="190">
          <template #default="{ row }">
            <div v-if="row.webhook_enabled" class="channel-line">
              Webhook
              <el-tag v-if="row.webhook_url_configured" size="small" type="success">URL 已配置</el-tag>
              <el-tag v-else size="small" type="danger">URL 未配置</el-tag>
            </div>
            <div v-if="row.smtp_enabled" class="channel-line">
              SMTP：{{ row.smtp_to || '未填收件人' }}
            </div>
            <span v-if="!row.webhook_enabled && !row.smtp_enabled" class="muted">未启用通道</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ row.updated_at ? formatDateTime(row.updated_at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link size="small" @click="openEditRule(row)">编辑</el-button>
            <el-button link size="small" type="danger" @click="removeRule(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- v1.35.0 规则变更历史：数据直接来自审计日志，展示字段前后值 -->
    <el-drawer
      v-model="historyVisible"
      :title="historyRule ? `变更历史 · ${historyRule.name}` : '变更历史'"
      size="640px"
      destroy-on-close
    >
      <div v-loading="historyLoading" class="rule-history-body" data-test="rule-history">
        <el-empty
          v-if="!historyLoading && historyEntries.length === 0"
          description="暂无变更记录；规则创建、编辑、启停与删除都会写入审计"
          :image-size="70"
        />
        <el-timeline v-else>
          <el-timeline-item
            v-for="entry in historyEntries"
            :key="entry.id"
            :timestamp="formatDateTime(entry.created_at)"
            placement="top"
          >
            <el-card shadow="never" class="history-card">
              <div class="history-head">
                <el-tag size="small" type="info">{{ entry.action }}</el-tag>
                <span class="history-operator">{{ entry.operator }}</span>
              </div>
              <div v-if="changeRows(entry).length" class="history-changes">
                <div v-for="change in changeRows(entry)" :key="change.field" class="history-change">
                  <span class="history-field">{{ change.field }}</span>
                  <span class="history-from">{{ change.from }}</span>
                  <span class="history-arrow">→</span>
                  <span class="history-to">{{ change.to }}</span>
                </div>
              </div>
              <div v-else class="history-hint">
                该操作无字段级前后值（如恢复默认阈值的汇总记录）
              </div>
            </el-card>
          </el-timeline-item>
        </el-timeline>
      </div>
    </el-drawer>

    <el-dialog
      v-model="ruleEditorVisible"
      :title="editingAlertRuleId === null ? '新增告警规则' : '编辑告警规则'"
      width="640px"
      destroy-on-close
    >
      <el-form label-width="110px" label-position="left">
        <el-form-item label="规则名称" required>
          <el-input v-model="alertRuleForm.name" maxlength="64" placeholder="如：华东机柜温度上限" />
        </el-form-item>
        <el-form-item label="指标" required>
          <el-select v-model="alertRuleForm.metric" style="width: 240px" @change="onMetricChange">
            <el-option
              v-for="option in METRIC_OPTIONS"
              :key="option.value"
              :label="`${option.label}（${option.unit}）`"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="比较方向" required>
          <el-radio-group v-model="alertRuleForm.op">
            <el-radio-button v-for="op in currentMetric.ops" :key="op" :value="op">
              {{ opLabel(op) }}阈值触发
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="阈值" required>
          <el-input-number
            v-model="alertRuleForm.threshold"
            :min="currentMetric.min"
            :max="currentMetric.max"
            :step="currentMetric.step"
            controls-position="right"
          />
          <span class="form-hint">{{ currentMetric.unit }}；等于阈值不告警</span>
        </el-form-item>
        <el-form-item label="级别" required>
          <el-radio-group v-model="alertRuleForm.level">
            <el-radio-button value="warning">warning（警告）</el-radio-button>
            <el-radio-button value="critical">critical（严重）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="生效范围" required>
          <el-radio-group
            :model-value="alertRuleForm.scope_mode"
            data-test="rule-scope-mode"
            @update:model-value="onScopeModeChange($event as 'global' | 'conditions')"
          >
            <el-radio-button value="global">全局</el-radio-button>
            <el-radio-button value="conditions">条件（且 / 或嵌套）</el-radio-button>
          </el-radio-group>
          <span class="form-hint">
            全局对所有设备生效；条件模式支持「标签 A 且 分组 B」这类组合
          </span>
        </el-form-item>
        <el-form-item v-if="alertRuleForm.scope_mode === 'conditions'" label="条件树" required>
          <RuleConditionBuilder
            v-if="alertRuleForm.conditions"
            v-model="alertRuleForm.conditions"
            :tag-options="ruleTagOptions"
            :group-options="ruleGroupOptions"
          />
          <span class="form-hint">
            同一条件内多个值为「或」；条件组为「且」/「或」可嵌套，最多 3 层
          </span>
        </el-form-item>
        <el-form-item label="连续次数">
          <el-input-number v-model="alertRuleForm.consecutive_count" :min="1" :max="20" />
          <span class="form-hint">连续 N 次越限才触发；1 表示首次越限即触发</span>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="alertRuleForm.priority" :min="1" :max="1000" />
          <span class="form-hint">同指标同级别多条命中时，数字小者优先</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="alertRuleForm.enabled" />
          <span class="form-hint">停用后保存也不参与判定</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleEditorVisible = false">取消</el-button>
        <el-button type="primary" :loading="ruleEditorSaving" @click="saveAlertRule">
          保存并生效
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="ruleDialogVisible"
      :title="editingRuleId === null ? '新增通知规则' : '编辑通知规则'"
      width="620px"
      destroy-on-close
    >
      <el-form label-width="120px" label-position="left">
        <el-form-item label="规则名称" required>
          <el-input v-model="ruleForm.name" maxlength="64" placeholder="如：华东值班" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="ruleForm.enabled" />
          <span class="form-hint">停用后不参与路由</span>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="ruleForm.priority" :min="1" :max="1000" />
          <span class="form-hint">数字越小越优先；同一告警只命中第一条</span>
        </el-form-item>
        <el-form-item label="级别门槛">
          <el-checkbox-group v-model="ruleForm.levels">
            <el-checkbox value="warning">warning（预警）</el-checkbox>
            <el-checkbox value="critical">critical（严重）</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="分组范围">
          <el-input v-model="ruleForm.groupsText" placeholder="留空表示全部分组；多个用逗号分隔" />
        </el-form-item>
        <el-form-item label="设备白名单">
          <el-input v-model="ruleForm.allowText" placeholder="留空不限；多个设备 ID 用逗号分隔" />
        </el-form-item>
        <el-form-item label="设备黑名单">
          <el-input v-model="ruleForm.denyText" placeholder="命中黑名单的设备不会命中本规则；优先于白名单" />
        </el-form-item>

        <el-divider content-position="left">接收人</el-divider>
        <el-form-item label="Webhook">
          <el-switch v-model="ruleForm.webhook_enabled" />
          <el-tag v-if="editingRule?.webhook_url_configured" size="small" type="success" class="inline-tag">
            URL 已配置
          </el-tag>
          <el-tag v-if="editingRule?.webhook_secret_configured" size="small" type="success" class="inline-tag">
            签名密钥已配置
          </el-tag>
        </el-form-item>
        <el-form-item v-if="ruleForm.webhook_enabled" label="Webhook URL">
          <el-input
            v-model="ruleForm.webhook_url"
            placeholder="https://...（编辑时留空保持原值）"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item v-if="ruleForm.webhook_enabled" label="签名密钥">
          <el-input
            v-model="ruleForm.webhook_secret"
            type="password"
            placeholder="用于 X-Vikings-Signature；编辑时留空保持原值"
            autocomplete="new-password"
            show-password
          />
        </el-form-item>
        <el-form-item label="SMTP">
          <el-switch v-model="ruleForm.smtp_enabled" />
          <span class="form-hint">SMTP 服务器与发件人沿用上方全局传输设置</span>
        </el-form-item>
        <el-form-item v-if="ruleForm.smtp_enabled" label="收件人">
          <el-input v-model="ruleForm.smtp_to" placeholder="值班邮箱，多个用逗号分隔" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="ruleSaving" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mwDialogVisible" title="新增维护窗口" width="640px">
      <el-form label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="mwForm.name" maxlength="64" placeholder="如：冷库机房割接" />
        </el-form-item>
        <el-form-item label="原因">
          <el-input
            v-model="mwForm.reason"
            type="textarea"
            :rows="2"
            maxlength="500"
            placeholder="可选，便于事后追溯"
          />
        </el-form-item>
        <el-form-item label="形态" required>
          <el-radio-group v-model="mwForm.mode">
            <el-radio-button value="once">一次性</el-radio-button>
            <el-radio-button value="weekly">每周周期</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="mwForm.mode === 'once'">
          <el-form-item label="开始（本地）" required>
            <el-date-picker
              v-model="mwForm.starts_at"
              type="datetime"
              value-format="YYYY-MM-DDTHH:mm:ss"
              placeholder="选择开始时间"
            />
          </el-form-item>
          <el-form-item label="结束（本地）" required>
            <el-date-picker
              v-model="mwForm.ends_at"
              type="datetime"
              value-format="YYYY-MM-DDTHH:mm:ss"
              placeholder="选择结束时间"
            />
          </el-form-item>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="提交时按浏览器本地时区转换为 UTC 存储；边界为 [开始, 结束)。"
          />
        </template>
        <template v-else>
          <el-form-item label="每周（UTC）" required>
            <el-select v-model="mwForm.weekday" style="width: 140px">
              <el-option
                v-for="(label, index) in WEEKDAY_LABELS"
                :key="index"
                :label="label"
                :value="index"
              />
            </el-select>
            <el-time-picker
              v-model="mwForm.start"
              format="HH:mm"
              value-format="HH:mm"
              placeholder="UTC 开始时间"
              style="margin-left: 8px; width: 140px"
            />
            <el-input-number
              v-model="mwForm.duration_minutes"
              :min="1"
              :max="10080"
              style="margin-left: 8px; width: 150px"
            />
            <span class="form-hint">分钟</span>
          </el-form-item>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="周期按 UTC 计算，不受部署机时区影响；跨天时窗口延续到次日。"
          />
        </template>
        <el-form-item label="静音范围" required>
          <el-radio-group v-model="mwForm.scope_mode">
            <el-radio-button value="global">全部设备</el-radio-button>
            <el-radio-button value="conditions">条件（且 / 或嵌套）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="mwForm.scope_mode === 'conditions'" label="条件树" required>
          <RuleConditionBuilder
            v-if="mwForm.conditions"
            v-model="mwForm.conditions"
            :tag-options="ruleTagOptions"
            :group-options="ruleGroupOptions"
          />
          <span class="form-hint">
            与告警规则共用同一套条件树；同一条件内多值为「或」，条件组可嵌套「且 / 或」
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mwDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mwSaving" @click="submitMaintenance">
          创建窗口
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="mwSummaryVisible"
      :title="`维护汇总 · ${mwSummaryWindow?.name ?? ''}`"
      width="720px"
    >
      <el-table :data="mwSummaries" size="small" empty-text="窗口尚未结束，暂无汇总">
        <el-table-column label="发生时间" min-width="210">
          <template #default="{ row }">
            {{ formatDateTime(row.occurrence_start) }} → {{ formatDateTime(row.occurrence_end) }}
          </template>
        </el-table-column>
        <el-table-column label="抑制" width="80">
          <template #default="{ row }">{{ row.suppressed_count }}</template>
        </el-table-column>
        <el-table-column label="设备数" width="80">
          <template #default="{ row }">{{ row.device_count }}</template>
        </el-table-column>
        <el-table-column label="critical" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.critical_suppressed" type="danger" size="small">
              {{ row.critical_suppressed }} 条被压
            </el-tag>
            <span v-else>0</span>
          </template>
        </el-table-column>
        <el-table-column label="指标分布" min-width="160">
          <template #default="{ row }">
            {{
              Object.entries(row.metrics)
                .map(([key, value]) => `${metricMeta(key as AlertMetric).label} ${value}`)
                .join(' / ') || '—'
            }}
          </template>
        </el-table-column>
        <el-table-column label="提前结束" width="90">
          <template #default="{ row }">{{ row.ended_early ? '是' : '否' }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog
      v-model="mwSuppressionVisible"
      :title="`抑制记录 · ${mwSuppressionWindow?.name ?? ''}`"
      width="760px"
    >
      <el-table :data="mwSuppressions" size="small" empty-text="暂无抑制记录">
        <el-table-column prop="device_id" label="设备" min-width="130" />
        <el-table-column label="指标" width="110">
          <template #default="{ row }">{{ metricMeta(row.metric).label }}</template>
        </el-table-column>
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="row.level === 'critical' ? 'danger' : 'warning'" size="small">
              {{ row.level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="实际 / 阈值" width="120">
          <template #default="{ row }">{{ row.value }} / {{ row.threshold }}</template>
        </el-table-column>
        <el-table-column prop="rule_name" label="规则" min-width="140" />
        <el-table-column label="时间" min-width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-empty v-if="!loading && !status" description="暂无系统状态数据" :image-size="90" />
  </div>
</template>

<style scoped>
.rule-history-body {
  min-height: 240px;
}

.history-card {
  border: 1px solid #e2e8f0;
}

.history-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.history-operator {
  font-size: 13px;
  color: #475569;
}

.history-changes {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.history-change {
  font-size: 12px;
  color: #475569;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}

.history-field {
  min-width: 70px;
  color: #0f172a;
  font-weight: 600;
}

.history-from {
  color: #94a3b8;
}

.history-arrow {
  color: #94a3b8;
}

.history-to {
  color: #0f172a;
}

.history-hint {
  font-size: 12px;
  color: #94a3b8;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.section-desc {
  margin-left: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.metric-row {
  row-gap: 12px;
}

.metric-card {
  padding: 14px 16px;
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
}

.metric-label {
  font-size: 13px;
  color: #64748b;
}

.metric-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.1;
  color: #0f172a;
}

.metric-value.ok {
  color: #16a34a;
}

.metric-value.bad {
  color: #dc2626;
}

.metric-extra {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
}

.notify-hint {
  margin-bottom: 12px;
}

.notify-form {
  max-width: 860px;
}

.form-hint {
  margin-left: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.inline-tag {
  margin-left: 10px;
}

.inline-text {
  margin: 0 8px;
  font-size: 12px;
  color: #64748b;
}

.host-input {
  width: 260px;
  margin-right: 8px;
}

.port-input {
  width: 110px;
  margin-right: 12px;
}

.half-input {
  width: 300px;
  margin-right: 8px;
}

.test-panel {
  padding-top: 12px;
  border-top: 1px dashed #e2e8f0;
}

.test-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.test-label {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
}

.test-results {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.updated-at {
  margin-top: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.muted {
  color: #94a3b8;
}

.channel-line {
  display: flex;
  align-items: center;
  gap: 6px;
  line-height: 1.8;
}
</style>
