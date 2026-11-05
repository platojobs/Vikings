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
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
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
import { formatDateTime } from '@/utils/format'
import type {
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

onMounted(() => {
  loadStatus()
  loadNotificationConfig()
  loadRules()
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

    <el-empty v-if="!loading && !status" description="暂无系统状态数据" :image-size="90" />
  </div>
</template>

<style scoped>
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
