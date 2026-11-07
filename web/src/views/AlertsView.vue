<script setup lang="ts">
/**
 * 告警中心：统计卡片 + 筛选列表 + 单条/批量确认（含处置备注）。
 * 订阅 device/system/alert，新告警实时插入列表头部并弹通知。
 * v1.21.0：批量确认复用 POST /api/alerts/bulk-ack，部分失败逐类展示；
 * viewer 只读，多选与确认按钮禁用并给出说明。
 * v1.30.0：新增时间范围筛选与「导出 CSV」，列表与导出共用同一套筛选参数。
 * v1.33.0：SLA 状态列与「已超时」筛选；值班交接（二次确认，operator/admin）。
 * v1.34.0：移除阈值抽屉（改由系统设置「告警规则」管理），新增「触发规则」列展示
 *          matched_rule，解释这条告警命中了哪条规则、阈值多少、实际值多少。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import {
  ackAlert,
  bulkAckAlerts,
  fetchAlerts,
  fetchAlertSummary,
  handoverAlerts,
} from '@/api/alerts'
import { fetchDeviceTags } from '@/api/devices'
import { exportAlerts, formatBytes } from '@/api/exports'
import { subscribe } from '@/services/mqtt'
import { canOperate } from '@/utils/permissions'
import { formatDateTime } from '@/utils/format'
import { summarizeBulkAck, type BulkAckSummary } from '@/utils/alertSummary'
import {
  alertLevelLabel,
  alertLevelTagType,
  alertMetricLabel,
  alertStatusLabel,
} from '@/utils/labels'
import type {
  AlertItem,
  AlertLevel,
  AlertMatchedRule,
  AlertSummary,
  DeviceTagStat,
  SlaState,
  SystemAlertMessage,
} from '@/types'

// ==================== 统计卡片 ====================

/** 只读角色不能确认告警；规则配置统一在系统设置中管理 */
const canWrite = computed(() => canOperate())

const summary = ref<AlertSummary>({ open: 0, critical_open: 0, today: 0, overdue_open: 0 })

async function loadSummary(): Promise<void> {
  try {
    summary.value = await fetchAlertSummary()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  }
}

// ==================== 告警列表 ====================

const query = reactive<{
  level: AlertLevel | ''
  status: 'open' | 'acked' | ''
  sla: SlaState | ''
  deviceId: string
  /** v1.35.0：标签多选，多个标签为「或」 */
  tags: string[]
}>({
  level: '',
  status: '',
  sla: '',
  deviceId: '',
  tags: [],
})

/** 标签筛选选项（含设备数），登录即可读 */
const tagStats = ref<DeviceTagStat[]>([])

async function loadTagStats(): Promise<void> {
  try {
    tagStats.value = await fetchDeviceTags()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  }
}
/** 时间范围筛选（v1.30.0）：与导出共用，null 表示不限时间 */
const range = ref<[Date, Date] | null>(null)

/** 导出状态：按钮 loading 期间禁用重复点击，进度显示已接收体积 */
const exporting = ref(false)
const exportLoaded = ref(0)

const alerts = ref<AlertItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const ackingId = ref('')

/** 多选状态：只允许选未处理告警，viewer 也能选但不允许提交 */
const tableRef = ref<{ clearSelection: () => void } | null>(null)
const selectedIds = ref<string[]>([])
/** 批量确认结果常驻展示，部分失败必须让操作者看到 */
const ackResult = ref<BulkAckSummary | null>(null)

const ackDialog = reactive<{
  visible: boolean
  mode: 'single' | 'bulk'
  note: string
  loading: boolean
}>({
  visible: false,
  mode: 'single',
  note: '',
  loading: false,
})
const singleTarget = ref<AlertItem | null>(null)

const ackDialogTitle = computed(() =>
  ackDialog.mode === 'bulk'
    ? `批量确认 ${selectedIds.value.length} 条告警`
    : '确认告警',
)

async function loadAlerts(): Promise<void> {
  loading.value = true
  try {
    const [start, end] = range.value ?? []
    const result = await fetchAlerts({
      level: query.level || undefined,
      status: query.status || undefined,
      sla: query.sla || undefined,
      deviceId: query.deviceId.trim() || undefined,
      // 不选标签时传 undefined，请求与升级前完全一致
      tag: query.tags.length ? query.tags : undefined,
      start: start ? start.getTime() : undefined,
      end: end ? end.getTime() : undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    alerts.value = result.items
    total.value = result.total
    clearSelection()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    loading.value = false
  }
}

function handleSelectionChange(rows: AlertItem[]): void {
  selectedIds.value = rows.map((row) => row.alert_id)
}

/** 已确认行不可再选，避免把「幂等跳过」混进本次待确认范围 */
function selectableRow(row: AlertItem): boolean {
  return row.status === 'open'
}

function clearSelection(): void {
  selectedIds.value = []
  tableRef.value?.clearSelection()
}

function handleSearch(): void {
  page.value = 1
  loadAlerts()
}

function handleReset(): void {
  query.level = ''
  query.status = ''
  query.sla = ''
  query.deviceId = ''
  query.tags = []
  range.value = null
  page.value = 1
  loadAlerts()
}

// ==================== SLA 展示与值班交接（v1.33.0） ====================

const SLA_LABELS: Record<SlaState, string> = {
  met: '达标',
  late: '超时达成',
  pending: '未超时',
  overdue: '已超时',
  unknown: '历史确认',
}

const SLA_TAG_TYPES: Record<SlaState, 'success' | 'warning' | 'info' | 'danger'> = {
  met: 'success',
  late: 'warning',
  pending: 'info',
  overdue: 'danger',
  unknown: 'info',
}

function slaLabel(state?: SlaState | null): string {
  return state ? SLA_LABELS[state] ?? state : '—'
}

function slaTagType(state?: SlaState | null): 'success' | 'warning' | 'info' | 'danger' {
  return state ? SLA_TAG_TYPES[state] ?? 'info' : 'info'
}

/** 未处理显示剩余/超出，已确认显示实际响应；历史确认无处置时间时明确提示 */
function slaDuration(row: AlertItem): string {
  if (row.sla_state === 'unknown') return '无处置时间'
  const seconds =
    row.sla_state === 'pending' || row.sla_state === 'overdue'
      ? row.sla_remaining_seconds
      : row.sla_response_seconds
  if (seconds === null || seconds === undefined) return '—'
  const minutes = Math.floor(Math.abs(seconds) / 60)
  const value = minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分`
  if (row.sla_state === 'pending') return `剩余 ${value}`
  if (row.sla_state === 'overdue') return `超出 ${value}`
  return `响应 ${value}`
}

const handoverDialog = reactive({
  visible: false,
  loading: false,
  to: '',
  fromUser: '',
  includeUnassigned: true,
  note: '',
})

function openHandover(): void {
  if (!canWrite.value) return
  handoverDialog.to = ''
  handoverDialog.fromUser = ''
  handoverDialog.includeUnassigned = true
  handoverDialog.note = ''
  handoverDialog.visible = true
}

/** 交接不可撤销，提交前二次确认（含交接范围与接收人） */
async function confirmHandover(): Promise<void> {
  const to = handoverDialog.to.trim()
  if (!to) {
    ElMessage.warning('请填写接收人（下一值班人）')
    return
  }
  const from = handoverDialog.fromUser.trim()
  const scope = handoverDialog.includeUnassigned
    ? `「${from || '当前登录人'}」名下及未指派的未处理告警`
    : `「${from || '当前登录人'}」名下的未处理告警`
  try {
    await ElMessageBox.confirm(
      `将把${scope}全部交接给「${to}」，交接后 SLA 计时与升级次数不会重置。是否继续？`,
      '确认值班交接',
      { type: 'warning', confirmButtonText: '确认交接', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  handoverDialog.loading = true
  try {
    const result = await handoverAlerts({
      to,
      from_user: from || undefined,
      include_unassigned: handoverDialog.includeUnassigned,
      note: handoverDialog.note.trim() || undefined,
    })
    if (result.count === 0) {
      ElMessage.info('没有需要交接的未处理告警')
    } else {
      ElMessage.success(
        `已交接 ${result.count} 条未处理告警给「${result.to}」，汇总通知已排队发送`,
      )
    }
    handoverDialog.visible = false
    await Promise.all([loadAlerts(), loadSummary()])
  } catch (error) {
    ElMessage.error((error as Error).message || '交接失败，请稍后重试')
  } finally {
    handoverDialog.loading = false
  }
}

/** 导出当前筛选范围；不做静默放宽，筛选为空就是不限 */
async function handleExport(): Promise<void> {
  if (!canWrite.value) {
    ElMessage.warning('只读账号不可导出，请使用 operator 或 admin 账号')
    return
  }
  const [start, end] = range.value ?? []
  if (start && end && end.getTime() <= start.getTime()) {
    ElMessage.warning('结束时间需晚于开始时间')
    return
  }
  exporting.value = true
  exportLoaded.value = 0
  try {
    const result = await exportAlerts(
      {
        deviceId: query.deviceId.trim() || undefined,
        level: query.level || undefined,
        status: query.status || undefined,
        tag: query.tags.length ? query.tags : undefined,
        start: start ? start.getTime() : undefined,
        end: end ? end.getTime() : undefined,
      },
      (progress) => {
        exportLoaded.value = progress.loadedBytes
      },
    )
    ElMessage.success(`已导出 ${result.filename}（${formatBytes(result.bytes)}）`)
  } catch (error) {
    ElMessage.error((error as Error).message || '导出失败，请稍后重试')
  } finally {
    exporting.value = false
  }
}

function handleSizeChange(): void {
  page.value = 1
  loadAlerts()
}

function openSingleAck(row: AlertItem): void {
  if (!canWrite.value) return
  singleTarget.value = row
  ackDialog.mode = 'single'
  ackDialog.note = ''
  ackDialog.visible = true
}

function openBulkAck(): void {
  if (!canWrite.value || selectedIds.value.length === 0) return
  ackDialog.mode = 'bulk'
  ackDialog.note = ''
  ackDialog.visible = true
}

/** 单条与批量共用同一个备注弹窗，语义与移动端保持一致 */
async function confirmAck(): Promise<void> {
  const note = ackDialog.note.trim()
  ackDialog.loading = true
  try {
    if (ackDialog.mode === 'single') {
      const target = singleTarget.value
      if (!target) return
      ackingId.value = target.alert_id
      const updated = await ackAlert(target.alert_id, note)
      Object.assign(target, updated)
      ackDialog.visible = false
      ElMessage.success(note ? '告警已确认，处置备注已记录' : '告警已确认')
      loadSummary()
    } else {
      const ids = [...selectedIds.value]
      const result = await bulkAckAlerts(ids, note)
      ackResult.value = summarizeBulkAck(result)
      ackDialog.visible = false
      await loadAlerts()
      loadSummary()
    }
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    ackDialog.loading = false
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

// ==================== 触发规则解释（v1.34.0） ====================

const METRIC_UNITS: Record<string, string> = {
  voltage: 'V',
  temperature: '℃',
  humidity: '%',
  rssi: 'dBm',
}

function ruleDirection(op?: 'lt' | 'gt'): string {
  return op === 'gt' ? '高于' : '低于'
}

/** 表格副行：方向 + 阈值（带单位） */
function ruleThresholdText(rule: AlertMatchedRule, metric: string): string {
  const unit = METRIC_UNITS[metric] ?? ''
  return `${ruleDirection(rule.op)} ${rule.threshold}${unit}`
}

/** 悬浮提示：完整回答「为什么触发」（规则名、条件、实际值、连续次数） */
function ruleExplain(row: AlertItem): string {
  const rule = row.matched_rule
  if (!rule) return 'v1.34.0 之前的历史告警没有规则快照，按当时的默认阈值判定'
  const unit = METRIC_UNITS[row.metric] ?? ''
  const ruleRef = rule.rule_id ? `规则 #${rule.rule_id}` : '默认规则'
  const continuity = rule.consecutive > 1 ? `，连续 ${rule.consecutive} 次越限` : ''
  return `${ruleRef}「${rule.rule_name}」：${alertMetricLabel(row.metric)}${ruleDirection(rule.op)} ` +
    `${rule.threshold}${unit}，实际 ${rule.value}${unit}${continuity}，按 ${rule.level} 级别产生告警`
}

onMounted(() => {
  loadSummary()
  loadAlerts()
  loadTagStats()
})

onUnmounted(() => {
  unsubscribeAlert()
})
</script>

<template>
  <div class="page alerts-page">
    <el-row :gutter="16" class="summary-row">
      <el-col :span="6">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value warning">{{ summary.open }}</div>
          <div class="summary-label">未处理告警</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value critical">{{ summary.overdue_open ?? 0 }}</div>
          <div class="summary-label">其中已超时</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="summary-card">
          <div class="summary-value critical">{{ summary.critical_open }}</div>
          <div class="summary-label">未处理严重告警</div>
        </el-card>
      </el-col>
      <el-col :span="6">
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
        <el-select v-model="query.sla" placeholder="全部 SLA" clearable class="filter-select">
          <el-option label="已超时" value="overdue" />
          <el-option label="未超时" value="pending" />
          <el-option label="达标" value="met" />
          <el-option label="超时达成" value="late" />
          <el-option label="历史确认（无处置时间）" value="unknown" />
        </el-select>
        <el-input
          v-model="query.deviceId"
          class="device-input"
          placeholder="按设备 ID 筛选"
          clearable
          @keyup.enter="handleSearch"
        />
        <el-select
          v-model="query.tags"
          multiple
          collapse-tags
          collapse-tags-tooltip
          clearable
          placeholder="全部标签"
          class="tag-select"
          data-test="alert-tag-filter"
        >
          <el-option
            v-for="item in tagStats"
            :key="item.tag"
            :label="`${item.tag}（${item.device_count}）`"
            :value="item.tag"
          />
        </el-select>
        <el-date-picker
          v-model="range"
          class="range-picker"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
        />
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
        <el-button
          :loading="exporting"
          :disabled="!canWrite"
          :title="canWrite ? '' : '只读账号不可导出'"
          @click="handleExport"
        >
          {{ exporting ? `导出中 ${formatBytes(exportLoaded)}` : '导出 CSV' }}
        </el-button>
        <div class="filter-spacer" />
        <el-button v-if="canWrite" type="primary" plain @click="openHandover">
          值班交接
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-alert
        v-if="ackResult"
        :type="ackResult.severity"
        :title="ackResult.title"
        :description="ackResult.detail"
        show-icon
        class="ack-result"
        @close="ackResult = null"
      />

      <div class="batch-bar">
        <span class="batch-count">已选择 {{ selectedIds.length }} 条未处理告警</span>
        <el-button
          type="primary"
          size="small"
          :disabled="!canWrite || selectedIds.length === 0"
          @click="openBulkAck"
        >
          批量确认
        </el-button>
        <span v-if="!canWrite" class="readonly-hint">
          当前为只读账号（viewer），可以查看但不能确认告警
        </span>
        <span v-else-if="selectedIds.length === 0" class="batch-tip">
          勾选未处理告警后可批量确认
        </span>
      </div>

      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="alerts"
        row-key="alert_id"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="46" :selectable="selectableRow" />
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
        <el-table-column label="触发规则" min-width="190">
          <template #default="{ row }">
            <template v-if="row.matched_rule">
              <el-tooltip :content="ruleExplain(row)" placement="top">
                <el-tag
                  size="small"
                  effect="plain"
                  :type="alertLevelTagType(row.matched_rule.level)"
                >
                  {{ row.matched_rule.rule_name }}
                </el-tag>
              </el-tooltip>
              <div class="rule-meta">
                {{ ruleThresholdText(row.matched_rule, row.metric) }} · 实际 {{ row.matched_rule.value }}
              </div>
            </template>
            <el-tooltip
              v-else
              content="v1.34.0 之前的历史告警按当时的硬编码默认阈值判定，没有规则快照"
              placement="top"
            >
              <span class="acked-hint">默认阈值（历史）</span>
            </el-tooltip>
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
        <el-table-column label="SLA" width="130">
          <template #default="{ row }">
            <el-tag :type="slaTagType(row.sla_state)" size="small" effect="plain">
              {{ slaLabel(row.sla_state) }}
            </el-tag>
            <div class="sla-meta">{{ slaDuration(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="责任人" width="110">
          <template #default="{ row }">
            <span v-if="row.assignee">{{ row.assignee }}</span>
            <span v-else class="acked-hint">未指派</span>
          </template>
        </el-table-column>
        <el-table-column label="处置信息" min-width="200">
          <template #default="{ row }">
            <template v-if="row.status === 'acked'">
              <div v-if="row.acked_by" class="ack-meta">
                {{ row.acked_by }}
                <span v-if="row.acked_at" class="ack-time">· {{ formatDateTime(row.acked_at) }}</span>
              </div>
              <div v-else class="acked-hint">历史确认（无处置记录）</div>
              <div v-if="row.ack_note" class="ack-note">{{ row.ack_note }}</div>
            </template>
            <span v-else class="acked-hint">—</span>
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
              @click="openSingleAck(row)"
            >
              确认
            </el-button>
            <span v-else class="acked-hint">{{ row.status === 'open' ? '只读' : '—' }}</span>
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

    <el-dialog v-model="handoverDialog.visible" title="值班交接" width="460px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="交接只转移责任归属，不重置 SLA 计时与升级次数"
        description="已超时的告警交接后仍标「已超时」，避免换班把欠账洗白。"
        class="handover-tip"
      />
      <el-form label-width="110px">
        <el-form-item label="接收人">
          <el-input v-model="handoverDialog.to" placeholder="下一值班人用户名" maxlength="64" />
        </el-form-item>
        <el-form-item label="交接人（可选）">
          <el-input
            v-model="handoverDialog.fromUser"
            placeholder="默认当前登录人"
            maxlength="64"
          />
        </el-form-item>
        <el-form-item label="包含未指派">
          <el-switch v-model="handoverDialog.includeUnassigned" />
          <span class="handover-hint">未指派告警也一并交给下一班</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="handoverDialog.note"
            type="textarea"
            :rows="2"
            maxlength="200"
            show-word-limit
            placeholder="交接说明（可选）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="handoverDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="handoverDialog.loading" @click="confirmHandover">
          交接
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="ackDialog.visible"
      :title="ackDialogTitle"
      width="460px"
      destroy-on-close
    >
      <div class="ack-dialog-body">
        <p v-if="ackDialog.mode === 'bulk'" class="ack-dialog-text">
          将确认选中的 {{ selectedIds.length }} 条未处理告警。部分告警可能已被他人确认或不存在，结果会按类别单独列出。
        </p>
        <p v-else class="ack-dialog-text">确认告警「{{ singleTarget?.message }}」。</p>
        <el-input
          v-model="ackDialog.note"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
          placeholder="可选：填写处置说明，例如「已联系现场，重启设备后恢复」"
        />
        <div class="ack-dialog-hint">备注与操作人、确认时间一并保存，并写入审计摘要。</div>
      </div>
      <template #footer>
        <el-button @click="ackDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="ackDialog.loading" @click="confirmAck">
          确认
        </el-button>
      </template>
    </el-dialog>
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

.tag-select {
  width: 200px;
}

.range-picker {
  width: 360px;
  max-width: 100%;
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

.sla-meta {
  margin-top: 2px;
  font-size: 12px;
  color: #64748b;
}

.handover-tip {
  margin-bottom: 12px;
}

.handover-hint {
  margin-left: 8px;
  font-size: 12px;
  color: #94a3b8;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.rule-meta {
  margin-top: 2px;
  font-size: 12px;
  color: #64748b;
}

/* v1.21.0 批量确认与处置信息 */
.ack-result {
  margin-bottom: 12px;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.batch-count {
  font-size: 13px;
  color: #475569;
}

.batch-tip,
.readonly-hint {
  font-size: 12px;
  color: #94a3b8;
}

.readonly-hint {
  color: #d97706;
}

.ack-meta {
  font-size: 13px;
  color: #334155;
}

.ack-time {
  color: #94a3b8;
}

.ack-note {
  margin-top: 2px;
  font-size: 12px;
  color: #64748b;
  word-break: break-word;
}

.ack-dialog-text {
  margin: 0 0 12px;
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
}

.ack-dialog-hint {
  margin-top: 8px;
  font-size: 12px;
  color: #94a3b8;
}
</style>
