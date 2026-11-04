<script setup lang="ts">
/**
 * 告警中心：统计卡片 + 筛选列表 + 确认操作 + 阈值在线配置。
 * 订阅 device/system/alert，新告警实时插入列表头部并弹通知。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElNotification } from 'element-plus'
import { ackAlert, fetchAlerts, fetchAlertSummary } from '@/api/alerts'
import { fetchThresholds, updateThresholds } from '@/api/rules'
import { subscribe } from '@/services/mqtt'
import { canOperate, isAdmin } from '@/utils/permissions'
import { formatDateTime } from '@/utils/format'
import {
  alertLevelLabel,
  alertLevelTagType,
  alertMetricLabel,
  alertStatusLabel,
} from '@/utils/labels'
import type {
  AlertItem,
  AlertLevel,
  AlertSummary,
  SystemAlertMessage,
  ThresholdsUpdateBody,
  ThresholdsView,
} from '@/types'

// ==================== 统计卡片 ====================

/** 只读角色不能确认告警；阈值配置仅管理员可见 */
const canWrite = computed(() => canOperate())
const isAdminOnly = computed(() => isAdmin())

const summary = ref<AlertSummary>({ open: 0, critical_open: 0, today: 0 })

async function loadSummary(): Promise<void> {
  try {
    summary.value = await fetchAlertSummary()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  }
}

// ==================== 告警列表 ====================

const query = reactive<{ level: AlertLevel | ''; status: 'open' | 'acked' | ''; deviceId: string }>({
  level: '',
  status: '',
  deviceId: '',
})

const alerts = ref<AlertItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const ackingId = ref('')

async function loadAlerts(): Promise<void> {
  loading.value = true
  try {
    const result = await fetchAlerts({
      level: query.level || undefined,
      status: query.status || undefined,
      deviceId: query.deviceId.trim() || undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    alerts.value = result.items
    total.value = result.total
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  loadAlerts()
}

function handleReset(): void {
  query.level = ''
  query.status = ''
  query.deviceId = ''
  page.value = 1
  loadAlerts()
}

function handleSizeChange(): void {
  page.value = 1
  loadAlerts()
}

async function handleAck(row: AlertItem): Promise<void> {
  ackingId.value = row.alert_id
  try {
    const updated = await ackAlert(row.alert_id)
    const target = alerts.value.find((a) => a.alert_id === row.alert_id)
    if (target) target.status = updated.status
    ElMessage.success('告警已确认')
    loadSummary()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    ackingId.value = ''
  }
}

// ==================== 实时告警推送 ====================

const unsubscribeAlert = subscribe('device/system/alert', (payload) => {
  const msg = payload as SystemAlertMessage
  if (!msg?.alertId) return

  // 通知弹窗：严重告警常驻，警告 4.5s 自动关闭
  ElNotification({
    title: `${alertLevelLabel(msg.level)}告警 · ${msg.deviceId}`,
    message: msg.message,
    type: msg.level === 'critical' ? 'error' : 'warning',
    duration: msg.level === 'critical' ? 0 : 4500,
  })

  // 只有当前筛选条件能匹配时才插入首行，避免污染列表
  if (page.value !== 1) return
  if (query.level && query.level !== msg.level) return
  if (query.status && query.status !== 'open') return
  if (query.deviceId.trim() && query.deviceId.trim() !== msg.deviceId) return

  alerts.value = [
    {
      alert_id: msg.alertId,
      device_id: msg.deviceId,
      level: msg.level,
      metric: msg.metric,
      value: msg.value,
      threshold: msg.threshold,
      message: msg.message,
      ai_generated: msg.aiGenerated,
      status: 'open',
      created_at: new Date(msg.ts).toISOString(),
    },
    ...alerts.value,
  ].slice(0, pageSize.value)
  total.value += 1
  loadSummary()
})

// ==================== 阈值在线配置 ====================

/** 表单字段定义：api 键 → 文案与取值约束（与后端 ThresholdsUpdateRequest 一致） */
const THRESHOLD_FIELDS = [
  { group: '电压（V）', key: 'voltage_warning', label: '警告阈值', min: 2.0, max: 4.5, step: 0.1 },
  { group: '电压（V）', key: 'voltage_critical', label: '严重阈值', min: 2.0, max: 4.5, step: 0.1 },
  { group: '温度（℃）', key: 'temperature_warning_high', label: '警告上限', min: -40, max: 85, step: 1 },
  { group: '温度（℃）', key: 'temperature_critical_high', label: '严重上限', min: -40, max: 85, step: 1 },
  { group: '温度（℃）', key: 'temperature_warning_low', label: '警告下限', min: -40, max: 85, step: 1 },
  { group: '温度（℃）', key: 'temperature_critical_low', label: '严重下限', min: -40, max: 85, step: 1 },
  { group: '湿度（%）', key: 'humidity_warning', label: '警告阈值', min: 0, max: 100, step: 1 },
  { group: '湿度（%）', key: 'humidity_critical', label: '严重阈值', min: 0, max: 100, step: 1 },
  { group: '信号强度（dBm）', key: 'rssi_warning', label: '警告阈值', min: -150, max: 0, step: 1 },
  { group: '信号强度（dBm）', key: 'rssi_critical', label: '严重阈值', min: -150, max: 0, step: 1 },
] as const

type ThresholdKey = (typeof THRESHOLD_FIELDS)[number]['key']

const thresholdDrawer = ref(false)
const thresholdLoading = ref(false)
const thresholdSaving = ref(false)
const thresholdForm = reactive<Record<ThresholdKey, number>>({
  voltage_warning: 0,
  voltage_critical: 0,
  temperature_warning_high: 0,
  temperature_critical_high: 0,
  temperature_warning_low: 0,
  temperature_critical_low: 0,
  humidity_warning: 0,
  humidity_critical: 0,
  rssi_warning: 0,
  rssi_critical: 0,
})
/** 打开抽屉时的快照，用于 diff 出实际修改的键 */
const thresholdSnapshot = ref<Record<ThresholdKey, number> | null>(null)
const thresholdSources = ref<Partial<Record<ThresholdKey, 'override' | 'env'>>>({})

/** 把后端分组视图拍平成 { api键: 值 } */
function flattenThresholds(view: ThresholdsView): Record<ThresholdKey, number> {
  return {
    voltage_warning: view.voltage.warning.value,
    voltage_critical: view.voltage.critical.value,
    temperature_warning_high: view.temperature.warning_high.value,
    temperature_critical_high: view.temperature.critical_high.value,
    temperature_warning_low: view.temperature.warning_low.value,
    temperature_critical_low: view.temperature.critical_low.value,
    humidity_warning: view.humidity.warning.value,
    humidity_critical: view.humidity.critical.value,
    rssi_warning: view.rssi.warning.value,
    rssi_critical: view.rssi.critical.value,
  }
}

function flattenSources(view: ThresholdsView): Partial<Record<ThresholdKey, 'override' | 'env'>> {
  return {
    voltage_warning: view.voltage.warning.source,
    voltage_critical: view.voltage.critical.source,
    temperature_warning_high: view.temperature.warning_high.source,
    temperature_critical_high: view.temperature.critical_high.source,
    temperature_warning_low: view.temperature.warning_low.source,
    temperature_critical_low: view.temperature.critical_low.source,
    humidity_warning: view.humidity.warning.source,
    humidity_critical: view.humidity.critical.source,
    rssi_warning: view.rssi.warning.source,
    rssi_critical: view.rssi.critical.source,
  }
}

async function openThresholdDrawer(): Promise<void> {
  thresholdDrawer.value = true
  thresholdLoading.value = true
  try {
    const view = await fetchThresholds()
    const flat = flattenThresholds(view)
    Object.assign(thresholdForm, flat)
    thresholdSnapshot.value = flat
    thresholdSources.value = flattenSources(view)
  } catch {
    thresholdDrawer.value = false
  } finally {
    thresholdLoading.value = false
  }
}

async function handleSaveThresholds(): Promise<void> {
  if (!thresholdSnapshot.value) return
  // 只提交发生变化的键，避免无意义的全量覆盖与审计噪音
  const body: ThresholdsUpdateBody = {}
  for (const field of THRESHOLD_FIELDS) {
    const key = field.key
    if (thresholdForm[key] !== thresholdSnapshot.value[key]) {
      body[key] = Number(thresholdForm[key])
    }
  }
  if (Object.keys(body).length === 0) {
    ElMessage.info('阈值没有变化')
    return
  }

  thresholdSaving.value = true
  try {
    const result = await updateThresholds(body)
    const flat = flattenThresholds(result.thresholds)
    Object.assign(thresholdForm, flat)
    thresholdSnapshot.value = flat
    thresholdSources.value = flattenSources(result.thresholds)
    ElMessage.success(`已更新 ${Object.keys(result.updated).length} 项阈值，即时生效`)
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    thresholdSaving.value = false
  }
}

function sourceTagType(source?: 'override' | 'env'): 'warning' | 'info' {
  return source === 'override' ? 'warning' : 'info'
}

function sourceLabel(source?: 'override' | 'env'): string {
  return source === 'override' ? '在线配置' : '环境变量'
}

onMounted(() => {
  loadSummary()
  loadAlerts()
})

onUnmounted(() => {
  unsubscribeAlert()
})
</script>

<template>
  <div class="page alerts-page">
    <el-row :gutter="16" class="summary-row">
      <el-col :span="8">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value warning">{{ summary.open }}</div>
          <div class="summary-label">未处理告警</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value critical">{{ summary.critical_open }}</div>
          <div class="summary-label">未处理严重告警</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value">{{ summary.today }}</div>
          <div class="summary-label">今日新增告警</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <div class="filter-bar">
        <el-select v-model="query.level" placeholder="全部级别" clearable class="filter-select">
          <el-option label="警告" value="warning" />
          <el-option label="严重" value="critical" />
        </el-select>
        <el-select v-model="query.status" placeholder="全部状态" clearable class="filter-select">
          <el-option label="未处理" value="open" />
          <el-option label="已确认" value="acked" />
        </el-select>
        <el-input
          v-model="query.deviceId"
          class="device-input"
          placeholder="按设备 ID 筛选"
          clearable
          @keyup.enter="handleSearch"
        />
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
        <div class="filter-spacer" />
        <el-button v-if="isAdminOnly" type="warning" plain @click="openThresholdDrawer">
          阈值配置
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="alerts" row-key="alert_id">
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="alertLevelTagType(row.level)" effect="dark" size="small">
              {{ alertLevelLabel(row.level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="device_id" label="设备 ID" min-width="120" />
        <el-table-column label="指标" width="100">
          <template #default="{ row }">{{ alertMetricLabel(row.metric) }}</template>
        </el-table-column>
        <el-table-column label="当前值 / 阈值" width="130">
          <template #default="{ row }">
            <span class="value-text">{{ row.value }}</span>
            <span class="threshold-text"> / {{ row.threshold }}</span>
          </template>
        </el-table-column>
        <el-table-column label="告警内容" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.message }}
            <el-tag v-if="row.ai_generated" size="small" type="success" effect="plain" class="ai-tag">
              AI
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'acked' ? 'info' : 'danger'" size="small" effect="plain">
              {{ alertStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="发生时间" min-width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'open' && canWrite"
              link
              type="primary"
              :loading="ackingId === row.alert_id"
              @click="handleAck(row)"
            >
              确认
            </el-button>
            <span v-else class="acked-hint">—</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无告警" :image-size="80" />
        </template>
      </el-table>

      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="loadAlerts"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>

    <el-drawer
      v-model="thresholdDrawer"
      title="告警阈值在线配置"
      size="480px"
      destroy-on-close
    >
      <div v-loading="thresholdLoading" class="threshold-body">
        <el-alert type="info" :closable="false" class="threshold-tip">
          修改即时生效并持久化，重启网关后仍然保留；标为「在线配置」的项优先于环境变量。
        </el-alert>

        <template v-for="groupName in ['电压（V）', '温度（℃）', '湿度（%）', '信号强度（dBm）']" :key="groupName">
          <div class="threshold-group">{{ groupName }}</div>
          <div
            v-for="field in THRESHOLD_FIELDS.filter((f) => f.group === groupName)"
            :key="field.key"
            class="threshold-item"
          >
            <span class="threshold-label">{{ field.label }}</span>
            <el-input-number
              v-model="thresholdForm[field.key]"
              :min="field.min"
              :max="field.max"
              :step="field.step"
              controls-position="right"
              class="threshold-input"
            />
            <el-tag :type="sourceTagType(thresholdSources[field.key])" size="small" effect="plain">
              {{ sourceLabel(thresholdSources[field.key]) }}
            </el-tag>
          </div>
        </template>

        <el-button
          type="primary"
          class="threshold-save"
          :loading="thresholdSaving"
          @click="handleSaveThresholds"
        >
          保存并生效
        </el-button>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.summary-row {
  margin-bottom: 16px;
}

.summary-card {
  text-align: center;
}

.summary-value {
  font-size: 30px;
  font-weight: 700;
  color: #0f172a;
}

.summary-value.warning {
  color: #d97706;
}

.summary-value.critical {
  color: #dc2626;
}

.summary-label {
  margin-top: 4px;
  font-size: 13px;
  color: #64748b;
}

.filter-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.filter-select {
  width: 130px;
}

.device-input {
  width: 200px;
}

.filter-spacer {
  flex: 1;
}

.table-card {
  margin-top: 16px;
}

.value-text {
  font-weight: 600;
  color: #dc2626;
}

.threshold-text {
  color: #94a3b8;
}

.ai-tag {
  margin-left: 6px;
}

.acked-hint {
  color: #cbd5e1;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.threshold-body {
  padding-bottom: 24px;
}

.threshold-tip {
  margin-bottom: 16px;
}

.threshold-group {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #475569;
}

.threshold-item {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.threshold-label {
  width: 72px;
  font-size: 13px;
  color: #64748b;
}

.threshold-input {
  flex: 1;
}

.threshold-save {
  width: 100%;
  margin-top: 20px;
}
</style>
