<script setup lang="ts">
/**
 * 设备列表：状态筛选 + 关键字搜索 + 分页。
 * 点击任意行打开抽屉，展示详情、历史数据折线图与指令历史。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  bulkCreateDevices,
  createDevice,
  deleteDevice,
  fetchDeviceCommands,
  fetchDeviceDetail,
  fetchDeviceGroups,
  fetchDeviceRecords,
  fetchDevices,
  issueDeviceCredential,
  revokeDeviceCredential,
  sendBulkCommand,
  sendCommand,
  updateDevice,
} from '@/api/devices'
import { BULK_DEVICE_LIMIT, bulkCommandLabel, summarizeBulkCommand } from '@/utils/batch'
import { formatDateTime, formatMetric } from '@/utils/format'
import { commandStateLabel, commandStateTagType } from '@/utils/labels'
import {
  CREDENTIAL_FINGERPRINT_NOTE,
  ONE_TIME_PASSWORD_NOTICE,
  UNISSUED_CREDENTIAL_NOTICE,
  buildIssueImpactText,
  buildRotationImpactText,
  credentialStatusLabel,
  credentialStatusTagType,
  useOneTimeCredential,
} from '@/utils/credential'
import DeviceStatusTag from '@/components/DeviceStatusTag.vue'
import SensorChart from '@/components/SensorChart.vue'
import { subscribe } from '@/services/mqtt'
import { canOperate, isAdmin } from '@/utils/permissions'
import type {
  BulkCommandType,
  CommandAckMessage,
  CommandBulkResult,
  CommandItem,
  CommandType,
  Device,
  DeviceBulkResult,
  DeviceCreateBody,
  DeviceDetail,
  DeviceGroupStat,
  DeviceStatus,
  SensorRecord,
} from '@/types'

const query = reactive<{ status: DeviceStatus | ''; keyword: string; group: string }>({
  status: '',
  keyword: '',
  group: '',
})

const devices = ref<Device[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const router = useRouter()

// ==================== 多选（跨页保留，上限与网关批量接口一致） ====================
const selectedMap = ref(new Map<string, Device>())
const selectedCount = computed(() => selectedMap.value.size)
const selectedDevices = computed(() => Array.from(selectedMap.value.values()))
const deviceTable = ref<{
  toggleRowSelection: (row: Device, selected: boolean) => void
  clearSelection: () => void
} | null>(null)

function toggleSelected(row: Device, checked: boolean): void {
  if (checked) {
    if (!selectedMap.value.has(row.device_id) && selectedMap.value.size >= BULK_DEVICE_LIMIT) {
      ElMessage.warning(`批量操作一次最多 ${BULK_DEVICE_LIMIT} 台，请分批处理`)
      deviceTable.value?.toggleRowSelection(row, false)
      return
    }
    selectedMap.value.set(row.device_id, row)
  } else {
    selectedMap.value.delete(row.device_id)
  }
  // Map 是响应式对象，替换引用触发 computed 更新
  selectedMap.value = new Map(selectedMap.value)
}

function handleSelect(row: Device, checked: boolean): void {
  toggleSelected(row, checked)
}

/** 全选/取消全选只作用于当前页，不影响其他页已选设备 */
function handleSelectAll(rows: Device[]): void {
  const allSelected = rows.every((row) => selectedMap.value.has(row.device_id))
  rows.forEach((row) => toggleSelected(row, !allSelected))
}

function clearSelection(): void {
  selectedMap.value = new Map()
  deviceTable.value?.clearSelection()
}

/** 分页/筛选后回填勾选状态，跨页保留选择 */
async function restoreSelection(): Promise<void> {
  await nextTick()
  for (const row of devices.value) {
    deviceTable.value?.toggleRowSelection(row, selectedMap.value.has(row.device_id))
  }
}

async function loadDevices(): Promise<void> {
  loading.value = true
  try {
    const result = await fetchDevices({
      status: query.status || undefined,
      keyword: query.keyword.trim() || undefined,
      group: query.group || undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    devices.value = result.items
    total.value = result.total
    await restoreSelection()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  loadDevices()
}

function handleReset(): void {
  query.status = ''
  query.keyword = ''
  query.group = ''
  page.value = 1
  loadDevices()
}

function handleSizeChange(): void {
  page.value = 1
  loadDevices()
}

// ==================== 分组与设备注册 ====================

const groups = ref<DeviceGroupStat[]>([])
/** 只读账号隐藏注册、编辑、下发等写操作，与后端 require_role(operator) 保持一致 */
const canWrite = computed(() => canOperate())
const isAdminOnly = computed(() => isAdmin())

async function loadGroups(): Promise<void> {
  try {
    groups.value = await fetchDeviceGroups()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  }
}

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const formSaving = ref(false)

/** 新建与编辑复用同一表单，device_id 在编辑态只读 */
const deviceForm = reactive<DeviceCreateBody>({
  device_id: '',
  name: '',
  model: 'VG-S100',
  firmware_version: '1.0.0',
  group_name: '默认分组',
})

function openCreateDevice(): void {
  formMode.value = 'create'
  editingId.value = ''
  Object.assign(deviceForm, {
    device_id: '',
    name: '',
    model: 'VG-S100',
    firmware_version: '1.0.0',
    group_name: '默认分组',
  })
  formVisible.value = true
}

function openEditDevice(row: Device): void {
  formMode.value = 'edit'
  editingId.value = row.device_id
  Object.assign(deviceForm, {
    device_id: row.device_id,
    name: row.name,
    model: row.model,
    firmware_version: row.firmware_version,
    group_name: row.group_name || '默认分组',
  })
  formVisible.value = true
}

async function handleSaveDevice(): Promise<void> {
  if (!deviceForm.name.trim()) {
    ElMessage.warning('请填写设备名称')
    return
  }
  if (formMode.value === 'create' && !/^[a-zA-Z0-9_-]{3,64}$/.test(deviceForm.device_id.trim())) {
    ElMessage.warning('设备 ID 需为 3~64 位字母、数字、_ 或 -')
    return
  }

  formSaving.value = true
  try {
    if (formMode.value === 'create') {
      const created = await createDevice({
        ...deviceForm,
        device_id: deviceForm.device_id.trim(),
        name: deviceForm.name.trim(),
        group_name: deviceForm.group_name.trim() || '默认分组',
      })
      ElMessage.success(`设备 ${created.device_id} 已注册`)
      page.value = 1
    } else {
      await updateDevice(editingId.value, {
        name: deviceForm.name.trim(),
        model: deviceForm.model,
        firmware_version: deviceForm.firmware_version,
        group_name: deviceForm.group_name.trim() || '默认分组',
      })
      ElMessage.success('设备信息已更新')
    }
    formVisible.value = false
    loadDevices()
    loadGroups()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    formSaving.value = false
  }
}

async function handleDeleteDevice(row: Device): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除设备 ${row.device_id}（${row.name}）？该操作不可恢复。`,
      '删除设备',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteDevice(row.device_id)
    devices.value = devices.value.filter((d) => d.device_id !== row.device_id)
    total.value -= 1
    ElMessage.success('设备已删除')
    loadGroups()
  } catch {
    // 错误提示已由 HTTP 层统一处理（存在历史数据时返回 409）
  }
}

// ==================== 批量导入 ====================

const bulkVisible = ref(false)
const bulkText = ref('')
const bulkSaving = ref(false)
const bulkResult = ref<DeviceBulkResult | null>(null)

function openBulkImport(): void {
  bulkText.value = ''
  bulkResult.value = null
  bulkVisible.value = true
}

/** 解析「设备ID,名称,型号,分组」四列，后两列可省略 */
function parseBulkRows(text: string): DeviceCreateBody[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
    // 允许首行是表头
    .filter((line) => !/^device_?id\s*,/i.test(line))
    .map((line) => {
      const [deviceId = '', name = '', model = 'VG-S100', group = '默认分组'] = line
        .split(',')
        .map((cell) => cell.trim())
      return {
        device_id: deviceId,
        name: name || `设备 ${deviceId}`,
        model: model || 'VG-S100',
        firmware_version: '1.0.0',
        group_name: group || '默认分组',
      }
    })
}

async function handleBulkImport(): Promise<void> {
  const rows = parseBulkRows(bulkText.value)
  if (rows.length === 0) {
    ElMessage.warning('请粘贴至少一行设备数据')
    return
  }
  const invalid = rows.find((row) => !/^[a-zA-Z0-9_-]{3,64}$/.test(row.device_id))
  if (invalid) {
    ElMessage.warning(`设备 ID 不合法：${invalid.device_id || '（空）'}`)
    return
  }

  bulkSaving.value = true
  try {
    bulkResult.value = await bulkCreateDevices(rows)
    ElMessage.success(`已注册 ${bulkResult.value.created.length} 台设备`)
    loadDevices()
    loadGroups()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    bulkSaving.value = false
  }
}

/** 选择 CSV 文件后读入文本框，用户仍可手动调整 */
function handleCsvFile(file: { raw?: File }): void {
  const raw = file.raw
  if (!raw) return
  const reader = new FileReader()
  reader.onload = () => {
    bulkText.value = String(reader.result ?? '')
  }
  reader.readAsText(raw, 'utf-8')
}

function exportDevicesCsv(): void {
  if (devices.value.length === 0) {
    ElMessage.warning('当前页无设备可导出')
    return
  }
  const header = ['device_id', 'name', 'model', 'firmware_version', 'group_name', 'status', 'last_report_at']
  const rows = devices.value.map((d) => [
    d.device_id,
    d.name,
    d.model,
    d.firmware_version,
    d.group_name,
    d.status,
    d.last_report_at ?? '',
  ])
  const csv = [header, ...rows]
    .map((line) => line.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(','))
    .join('\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `devices_${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已导出 ${devices.value.length} 台设备`)
}

// ==================== 详情抽屉 ====================

const drawerVisible = ref(false)
const drawerLoading = ref(false)
const detail = ref<DeviceDetail | null>(null)
const historyRecords = ref<SensorRecord[]>([])
const commands = ref<CommandItem[]>([])

async function openDevice(row: Device, column?: { type?: string }): Promise<void> {
  if (column?.type === 'selection') return // 勾选不打开详情抽屉
  drawerVisible.value = true
  drawerLoading.value = true
  detail.value = null
  historyRecords.value = []
  commands.value = []

  try {
    const [deviceDetail, records, commandList] = await Promise.all([
      fetchDeviceDetail(row.device_id),
      fetchDeviceRecords(row.device_id, { limit: 200 }),
      fetchDeviceCommands(row.device_id),
    ])
    detail.value = deviceDetail
    historyRecords.value = records
    commands.value = commandList
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    drawerLoading.value = false
  }
}

// ==================== MQTT 凭证 ====================

const credentialSubmitting = ref(false)
const oneTimeCredential = useOneTimeCredential()
const oneTimeOperation = ref<'issue' | 'rotate'>('issue')
const credentialReloadFailed = ref(false)
const credentialReloadDetail = ref('')

const credentialStatus = computed(() => {
  if (!detail.value) return 'missing' as const
  return (
    detail.value.mqtt_credential_status ??
    (detail.value.mqtt_credential_issued_at ? 'active' : 'missing')
  )
})

const credentialStatusText = computed(() => credentialStatusLabel(credentialStatus.value))
const credentialStatusTag = computed(() => credentialStatusTagType(credentialStatus.value))

async function refreshDetail(): Promise<void> {
  if (!detail.value) return
  detail.value = await fetchDeviceDetail(detail.value.device_id)
}

async function handleIssueOrRotateCredential(): Promise<void> {
  if (!detail.value) return
  const deviceId = detail.value.device_id
  const isRotation = credentialStatus.value === 'active'

  try {
    await ElMessageBox.confirm(
      isRotation ? buildRotationImpactText(deviceId) : buildIssueImpactText(deviceId),
      isRotation ? '轮换设备凭证' : '签发设备凭证',
      {
        type: 'warning',
        confirmButtonText: isRotation ? '确认轮换' : '确认签发',
        cancelButtonText: '取消',
        distinguishCancelAndClose: true,
        customClass: 'credential-confirm',
      },
    )
  } catch {
    return
  }

  credentialSubmitting.value = true
  try {
    const result = await issueDeviceCredential(deviceId)
    oneTimeCredential.show(result.password)
    oneTimeOperation.value = result.operation
    credentialReloadFailed.value = !result.reloaded
    credentialReloadDetail.value = result.reload_detail
    ElMessage.success(isRotation ? '新凭证已写入' : '凭证已签发')
    await refreshDetail()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    credentialSubmitting.value = false
  }
}

async function handleRevokeCredential(): Promise<void> {
  if (!detail.value) return
  const deviceId = detail.value.device_id
  try {
    await ElMessageBox.confirm(
      [
        `确认吊销设备 ${deviceId} 的 MQTT 凭证？`,
        '',
        '吊销后 Broker 账号被删除，设备立即无法接入，直到重新签发凭证；',
        '若 Broker 重载失败，旧凭证可能仍然有效，需按提示人工重载。',
      ].join('\n'),
      '吊销设备凭证',
      {
        type: 'warning',
        confirmButtonText: '确认吊销',
        cancelButtonText: '取消',
        distinguishCancelAndClose: true,
      },
    )
  } catch {
    return
  }

  credentialSubmitting.value = true
  try {
    const result = await revokeDeviceCredential(deviceId)
    if (result.reloaded) {
      ElMessage.success('凭证已吊销，Broker 已重载')
    } else {
      ElMessage.warning(
        `凭证已标记吊销，但 Broker 未重载，旧凭证可能仍然有效：${result.reload_detail}`,
      )
    }
    await refreshDetail()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    credentialSubmitting.value = false
  }
}

async function copyOneTimePassword(): Promise<void> {
  try {
    if (!oneTimeCredential.password.value) return
    await navigator.clipboard.writeText(oneTimeCredential.password.value)
    ElMessage.success('口令已复制到剪贴板')
  } catch {
    ElMessage.warning('浏览器拒绝了剪贴板访问，请手动选中口令复制')
  }
}

/** 明确确认已保存后才允许关闭：关闭即清空内存中的口令 */
function acknowledgeOneTimePassword(): void {
  oneTimeCredential.acknowledge()
  ElMessage.success('已关闭一次性口令窗口；如需再次查看只能重新轮换')
}

// ==================== 指令下发面板 ====================

const commandType = ref<CommandType>('config')
const submittingCmd = ref(false)

const calibrateForm = reactive({
  temperatureOffset: 0,
  humidityOffset: 0,
})

const configForm = reactive({
  reportIntervalSec: 30,
  sleepSec: 15,
})

/** 各指令类型的提交载荷构造 */
function buildPayload(type: CommandType): Record<string, unknown> {
  switch (type) {
    case 'calibrate':
      return {
        temperatureOffset: Number(calibrateForm.temperatureOffset) || 0,
        humidityOffset: Number(calibrateForm.humidityOffset) || 0,
      }
    case 'config':
      return {
        reportIntervalSec: Number(configForm.reportIntervalSec) || 30,
        sleepSec: Number(configForm.sleepSec) || 15,
      }
    case 'reboot':
      return {}
    default:
      return {}
  }
}

async function handleSendCommand(): Promise<void> {
  if (!detail.value) return
  submittingCmd.value = true
  try {
    const cmd = await sendCommand(
      detail.value.device_id,
      commandType.value,
      buildPayload(commandType.value),
    )
    // 新指令插入历史列表头部，等待 MQTT 回执推进状态
    commands.value = [cmd, ...commands.value.filter((c) => c.cmd_id !== cmd.cmd_id)]
    ElMessage.success(`指令已下发：${commandLabel(commandType.value)}`)
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    submittingCmd.value = false
  }
}

function commandLabel(type: CommandType): string {
  switch (type) {
    case 'calibrate':
      return '传感器校准'
    case 'reboot':
      return '设备重启'
    case 'config':
      return '运行参数配置'
    default:
      return type
  }
}

// ==================== 批量指令（v1.28.0） ====================

const batchCommandVisible = ref(false)
const batchCommandType = ref<BulkCommandType>('config')
const batchSubmitting = ref(false)
const batchResult = ref<CommandBulkResult | null>(null)
const batchSummary = computed(() =>
  batchResult.value ? summarizeBulkCommand(batchResult.value) : null,
)

function openBatchCommand(): void {
  if (selectedCount.value === 0) {
    ElMessage.warning('请先勾选需要操作的设备')
    return
  }
  batchResult.value = null
  batchCommandType.value = 'config'
  batchCommandVisible.value = true
}

function newRequestId(): string {
  return `batch-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

async function handleBatchCommand(): Promise<void> {
  if (selectedCount.value === 0) return
  const label = bulkCommandLabel(batchCommandType.value)
  try {
    await ElMessageBox.confirm(
      `将向 ${selectedCount.value} 台设备下发「${label}」，指令下发后不可撤回。\n` +
        '离线或未签发有效凭证的设备会被服务端跳过，并在结果中逐台列出原因。',
      '批量指令确认',
      { type: 'warning', confirmButtonText: '确认下发', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  batchSubmitting.value = true
  try {
    batchResult.value = await sendBulkCommand(
      selectedDevices.value.map((device) => device.device_id),
      batchCommandType.value,
      buildPayload(batchCommandType.value),
      newRequestId(),
    )
    const summary = summarizeBulkCommand(batchResult.value)
    ElMessage.success(
      `批量指令完成：成功 ${summary.groups[0].count} 台 / 失败 ${summary.groups[1].count} 台 / 跳过 ${summary.groups[2].count} 台`,
    )
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    batchSubmitting.value = false
  }
}

/** 批量 OTA 不重复造表单：带选中设备跳转 OTA 页，复用既有任务端点 */
function goBatchOta(): void {
  if (selectedCount.value === 0) {
    ElMessage.warning('请先勾选需要升级的设备')
    return
  }
  if (selectedCount.value > BULK_DEVICE_LIMIT) {
    ElMessage.warning(`一次最多 ${BULK_DEVICE_LIMIT} 台，请分批处理`)
    return
  }
  const ids = selectedDevices.value.map((device) => device.device_id)
  router.push({ name: 'ota', query: { deviceIds: ids.join(',') } })
}

// ==================== 历史数据 CSV 导出 ====================

function exportCsv(): void {
  if (!detail.value || historyRecords.value.length === 0) {
    ElMessage.warning('暂无数据可导出')
    return
  }
  const header = ['seq', 'ts', 'temperature', 'humidity', 'voltage', 'rssi', 'battery']
  const rows = historyRecords.value.map((r) => [
    r.seq,
    new Date(r.ts).toISOString(),
    r.temperature,
    r.humidity,
    r.voltage,
    r.rssi,
    r.battery,
  ])
  const csv = [header, ...rows]
    .map((line) => line.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(','))
    .join('\n')
  // 加 BOM 让 Excel 正确识别 UTF-8 中文
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${detail.value.device_id}_sensor_${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已导出 ${historyRecords.value.length} 条数据`)
}

onMounted(() => {
  loadDevices()
  loadGroups()
})

// ==================== 实时状态刷新 ====================
// 订阅 device/+/status，设备上下线时直接更新对应行，无需整页重拉。
// 同步抽屉里详情对象的状态，保持抽屉与列表一致。
function deviceIdFromTopic(topic: string): string {
  const parts = topic.split('/')
  return parts.length >= 2 ? parts[1] : ''
}

const unsubscribeStatus = subscribe('device/+/status', (payload, topic) => {
  const deviceId = deviceIdFromTopic(topic)
  if (!deviceId) return
  const data = payload as { status?: DeviceStatus }
  if (!data?.status) return

  const row = devices.value.find((d) => d.device_id === deviceId)
  if (row) {
    row.status = data.status
  }
  if (detail.value?.device_id === deviceId) {
    detail.value.status = data.status
  }
})

// 订阅 device/+/sensor/report，更新最后上报时间
const unsubscribeReport = subscribe('device/+/sensor/report', (payload, topic) => {
  const deviceId = deviceIdFromTopic(topic)
  if (!deviceId) return
  const data = payload as { ts?: number }
  if (!data?.ts) return
  const row = devices.value.find((d) => d.device_id === deviceId)
  if (row) {
    row.last_report_at = new Date(data.ts).toISOString()
  }
})

// 订阅 device/+/cmd/ack，实时推进抽屉里指令历史的状态与进度
const unsubscribeAck = subscribe('device/+/cmd/ack', (payload) => {
  const ack = payload as CommandAckMessage
  if (!ack?.cmdId) return
  const target = commands.value.find((c) => c.cmd_id === ack.cmdId)
  if (!target) return
  const rank = (s: string) => ({ issued: 0, received: 1, progress: 2, success: 3, failed: 3 }[s] ?? 0)
  if (rank(ack.state) >= rank(target.state) || ack.state === 'failed') {
    target.state = ack.state
  }
  if (typeof ack.progress === 'number') {
    target.progress = Math.max(target.progress, ack.progress)
  }
  if (ack.message) target.message = ack.message
})

onUnmounted(() => {
  unsubscribeStatus()
  unsubscribeReport()
  unsubscribeAck()
})
</script>

<template>
  <div class="page devices-page">
    <el-card shadow="never">
      <div class="filter-bar">
        <el-select v-model="query.status" placeholder="全部状态" clearable class="status-select">
          <el-option label="在线" value="online" />
          <el-option label="离线" value="offline" />
        </el-select>
        <el-select v-model="query.group" placeholder="全部分组" clearable class="group-select">
          <el-option
            v-for="item in groups"
            :key="item.group_name"
            :label="`${item.group_name}（${item.total}）`"
            :value="item.group_name"
          />
        </el-select>
        <el-input
          v-model="query.keyword"
          class="keyword-input"
          placeholder="搜索设备 ID / 名称 / 型号"
          clearable
          @keyup.enter="handleSearch"
        />
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
        <span class="total-hint">共 {{ total }} 台设备</span>
        <div class="filter-spacer" />
        <template v-if="canWrite">
          <el-button plain :disabled="selectedCount === 0" @click="openBatchCommand">
            批量指令{{ selectedCount ? `（${selectedCount}）` : '' }}
          </el-button>
          <el-button plain :disabled="selectedCount === 0" @click="goBatchOta">
            批量 OTA{{ selectedCount ? `（${selectedCount}）` : '' }}
          </el-button>
          <el-button plain @click="openBulkImport">批量导入</el-button>
          <el-button type="primary" @click="openCreateDevice">新建设备</el-button>
        </template>
        <el-button v-if="canWrite && selectedCount > 0" link @click="clearSelection">清空选择</el-button>
        <el-button plain @click="exportDevicesCsv">导出 CSV</el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-table
        ref="deviceTable"
        v-loading="loading"
        :data="devices"
        row-key="device_id"
        class="device-table"
        @row-click="openDevice"
        @select="handleSelect"
        @select-all="handleSelectAll"
      >
        <el-table-column v-if="canWrite" type="selection" width="46" reserve-selection />
        <el-table-column prop="device_id" label="设备 ID" min-width="120" />
        <el-table-column prop="name" label="设备名称" min-width="160" />
        <el-table-column prop="model" label="型号" min-width="110" />
        <el-table-column label="分组" width="130">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" type="info">
              {{ row.group_name || '默认分组' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="firmware_version" label="固件版本" width="110" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <DeviceStatusTag :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="最后上报时间" min-width="170">
          <template #default="{ row }">
            {{ formatDateTime(row.last_report_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDevice(row)">详情</el-button>
            <el-button v-if="canWrite" link type="primary" @click.stop="openEditDevice(row)">
              编辑
            </el-button>
            <el-button
              v-if="isAdminOnly"
              link
              type="danger"
              @click.stop="handleDeleteDevice(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无设备数据" :image-size="80" />
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
          @current-change="loadDevices"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      title="设备详情"
      size="720px"
      class="device-drawer"
      destroy-on-close
    >
      <div v-loading="drawerLoading" class="drawer-body">
        <el-descriptions v-if="detail" :column="2" border class="detail-desc">
          <el-descriptions-item label="设备 ID">{{ detail.device_id }}</el-descriptions-item>
          <el-descriptions-item label="设备名称">{{ detail.name }}</el-descriptions-item>
          <el-descriptions-item label="型号">{{ detail.model }}</el-descriptions-item>
          <el-descriptions-item label="分组">{{ detail.group_name || '默认分组' }}</el-descriptions-item>
          <el-descriptions-item label="固件版本">{{ detail.firmware_version }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <DeviceStatusTag :status="detail.status" />
          </el-descriptions-item>
          <el-descriptions-item label="最后上报">
            {{ formatDateTime(detail.last_report_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最后上线">
            {{ formatDateTime(detail.last_online_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最新温度">
            {{ detail.latest ? `${formatMetric(detail.latest.temperature)} ℃` : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="最新湿度">
            {{ detail.latest ? `${formatMetric(detail.latest.humidity)} %` : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="最新电压">
            {{ detail.latest ? `${formatMetric(detail.latest.voltage, 2)} V` : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="电量">
            {{ detail.latest ? `${detail.latest.battery} %` : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="信号强度">
            {{ detail.latest ? `${detail.latest.rssi} dBm` : '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <section v-if="detail" class="drawer-section credential-section" data-test="device-credential">
          <div class="section-header">
            <h3>MQTT 凭证</h3>
            <span class="section-hint">设备接入 Broker 的用户名与口令状态</span>
          </div>

          <el-alert
            v-if="credentialStatus === 'missing'"
            :title="UNISSUED_CREDENTIAL_NOTICE"
            description="请点击下方「签发凭证」生成用户名/口令；签发后口令只显示一次。"
            type="warning"
            :closable="false"
            show-icon
            class="credential-alert"
          />
          <el-alert
            v-else-if="credentialStatus === 'revoked'"
            title="该设备凭证已吊销，当前无法接入 Broker"
            description="如需恢复接入，请重新签发凭证并更新设备配置。"
            type="error"
            :closable="false"
            show-icon
            class="credential-alert"
          />

          <el-descriptions :column="2" border class="credential-desc">
            <el-descriptions-item label="凭证状态">
              <el-tag :type="credentialStatusTag" size="small">{{ credentialStatusText }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="MQTT 用户名">
              {{ detail.mqtt_username || detail.device_id }}
            </el-descriptions-item>
            <el-descriptions-item label="凭证版本">
              {{ detail.mqtt_credential_version || 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="指纹前缀">
              <code v-if="detail.mqtt_credential_fingerprint_prefix">
                {{ detail.mqtt_credential_fingerprint_prefix }}…
              </code>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item label="首次签发">
              {{ formatDateTime(detail.mqtt_credential_issued_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="最近轮换">
              {{ formatDateTime(detail.mqtt_credential_rotated_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="吊销时间">
              {{ formatDateTime(detail.mqtt_credential_revoked_at) }}
            </el-descriptions-item>
          </el-descriptions>
          <p class="section-hint credential-fingerprint-note">{{ CREDENTIAL_FINGERPRINT_NOTE }}</p>

          <div class="credential-actions">
            <el-button
              v-if="credentialStatus !== 'active'"
              type="primary"
              :loading="credentialSubmitting"
              :disabled="!canWrite"
              @click="handleIssueOrRotateCredential"
            >
              {{ credentialStatus === 'revoked' ? '重新签发凭证' : '签发凭证' }}
            </el-button>
            <template v-else>
              <el-button
                type="warning"
                :loading="credentialSubmitting"
                :disabled="!isAdminOnly"
                @click="handleIssueOrRotateCredential"
              >
                轮换凭证
              </el-button>
              <el-button
                type="danger"
                plain
                :loading="credentialSubmitting"
                :disabled="!isAdminOnly"
                @click="handleRevokeCredential"
              >
                吊销凭证
              </el-button>
              <span v-if="!isAdminOnly" class="section-hint">
                轮换/吊销会导致设备掉线，仅管理员可执行；operator 仅可首次签发
              </span>
            </template>
          </div>
        </section>

        <section class="drawer-section">
          <div class="section-header">
            <h3>下发指令</h3>
            <span class="section-hint">网关落库后经 MQTT 推送给设备，设备在唤醒窗口内回执</span>
          </div>
          <el-alert
            v-if="!canWrite"
            title="当前账号为只读角色，无法下发指令。如需执行运维操作，请联系管理员调整角色。"
            type="info"
            :closable="false"
            show-icon
            class="readonly-tip"
          />
          <el-radio-group v-model="commandType" class="cmd-type-group">
            <el-radio-button label="config">运行参数</el-radio-button>
            <el-radio-button label="calibrate">传感器校准</el-radio-button>
            <el-radio-button label="reboot">设备重启</el-radio-button>
          </el-radio-group>

          <div v-if="commandType === 'calibrate'" class="cmd-form">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="温度偏移 (℃)">
                  <el-input-number
                    v-model="calibrateForm.temperatureOffset"
                    :step="0.1"
                    :precision="1"
                    controls-position="right"
                    class="full-width"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="湿度偏移 (%RH)">
                  <el-input-number
                    v-model="calibrateForm.humidityOffset"
                    :step="0.1"
                    :precision="1"
                    controls-position="right"
                    class="full-width"
                  />
                </el-form-item>
              </el-col>
            </el-row>
          </div>

          <div v-else-if="commandType === 'config'" class="cmd-form">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="上报间隔 (秒)">
                  <el-input-number
                    v-model="configForm.reportIntervalSec"
                    :min="1"
                    :max="3600"
                    controls-position="right"
                    class="full-width"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="休眠时长 (秒)">
                  <el-input-number
                    v-model="configForm.sleepSec"
                    :min="1"
                    :max="86400"
                    controls-position="right"
                    class="full-width"
                  />
                </el-form-item>
              </el-col>
            </el-row>
          </div>

          <div v-else class="cmd-form">
            <el-alert
              title="重启指令会重置设备上报序号 seq 并立即重新上线，OTA 进行中请勿重启。"
              type="warning"
              :closable="false"
              show-icon
            />
          </div>

          <el-button
            type="primary"
            :loading="submittingCmd"
            :disabled="!detail || !canWrite"
            @click="handleSendCommand"
          >
            下发{{ commandLabel(commandType) }}指令
          </el-button>
        </section>

        <section class="drawer-section">
          <div class="section-header">
            <h3>历史数据</h3>
            <div class="section-actions">
              <span class="section-hint">最近 {{ historyRecords.length }} 条</span>
              <el-button
                size="small"
                :disabled="!historyRecords.length"
                @click="exportCsv"
              >
                导出 CSV
              </el-button>
            </div>
          </div>
          <SensorChart v-if="historyRecords.length" :records="historyRecords" height="260px" />
          <el-empty v-else description="暂无历史数据" :image-size="70" />
        </section>

        <section class="drawer-section">
          <div class="section-header">
            <h3>指令历史</h3>
            <span class="section-hint">共 {{ commands.length }} 条</span>
          </div>
          <el-table v-if="commands.length" :data="commands" size="small" row-key="cmd_id">
            <el-table-column prop="cmd_id" label="指令 ID" min-width="140" show-overflow-tooltip />
            <el-table-column prop="type" label="类型" width="90" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="commandStateTagType(row.state)" size="small">
                  {{ commandStateLabel(row.state) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="进度" width="130">
              <template #default="{ row }">
                <el-progress :percentage="row.progress" :stroke-width="8" />
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.message || '—' }}</template>
            </el-table-column>
            <el-table-column label="下发时间" min-width="150">
              <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无指令记录" :image-size="70" />
        </section>
      </div>
    </el-drawer>

    <el-dialog
      v-model="formVisible"
      :title="formMode === 'create' ? '新建设备' : `编辑设备 · ${editingId}`"
      width="460px"
    >
      <el-form label-position="top">
        <el-form-item label="设备 ID">
          <el-input
            v-model="deviceForm.device_id"
            :disabled="formMode === 'edit'"
            placeholder="如 dev-1001，注册后不可修改"
          />
        </el-form-item>
        <el-form-item label="设备名称">
          <el-input v-model="deviceForm.name" placeholder="如 包装线温湿度传感器" />
        </el-form-item>
        <el-form-item label="型号">
          <el-input v-model="deviceForm.model" placeholder="VG-S100" />
        </el-form-item>
        <el-form-item label="固件版本">
          <el-input v-model="deviceForm.firmware_version" placeholder="1.0.0" />
        </el-form-item>
        <el-form-item label="分组">
          <el-input v-model="deviceForm.group_name" placeholder="默认分组" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="formSaving" @click="handleSaveDevice">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="bulkVisible" title="批量注册设备" width="620px">
      <el-alert type="info" :closable="false" class="bulk-tip">
        每行一台设备，格式：<code>设备ID,名称,型号,分组</code>，后两列可省略，重复 ID 自动跳过。
      </el-alert>

      <div class="bulk-toolbar">
        <el-upload
          :auto-upload="false"
          :show-file-list="false"
          accept=".csv,text/csv"
          :on-change="handleCsvFile"
        >
          <el-button size="small" plain>从 CSV 文件导入</el-button>
        </el-upload>
        <el-button size="small" link @click="bulkText = 'dev-1001,一号线温湿度,一号车间\ndev-1002,二号线温湿度,二号车间'">
          填入示例
        </el-button>
      </div>

      <el-input
        v-model="bulkText"
        type="textarea"
        :rows="8"
        placeholder="dev-1001,一号线温湿度,一号车间&#10;dev-1002,二号线温湿度,二号车间"
      />

      <el-alert
        v-if="bulkResult"
        :title="`注册完成：新增 ${bulkResult.created.length} 台，跳过 ${bulkResult.skipped.length} 台`"
        :description="bulkResult.skipped.length ? `已跳过已存在设备：${bulkResult.skipped.slice(0, 6).join('、')}` : ''"
        type="success"
        :closable="false"
        show-icon
        class="bulk-result"
      />

      <template #footer>
        <el-button @click="bulkVisible = false">关闭</el-button>
        <el-button type="primary" :loading="bulkSaving" @click="handleBulkImport">开始导入</el-button>
      </template>
    </el-dialog>

    <!--
      一次性口令：禁用遮罩点击、ESC 与右上角关闭，只能通过「我已保存」关闭；
      关闭即清空内存，服务端只存指纹，无法再次查询。
    -->
    <el-dialog
      :model-value="oneTimeCredential.visible.value"
      title="请立即保存新凭证口令"
      width="560px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <el-alert
        :title="ONE_TIME_PASSWORD_NOTICE"
        type="warning"
        :closable="false"
        show-icon
      />
      <div class="one-time-password" data-test="one-time-password">
        <span class="password-label">
          MQTT 口令（{{ oneTimeOperation === 'rotate' ? '轮换后' : '签发' }}）
        </span>
        <code class="password-value">{{ oneTimeCredential.password.value }}</code>
      </div>
      <div class="credential-actions">
        <el-button type="primary" plain @click="copyOneTimePassword">复制口令</el-button>
        <span class="section-hint">请先粘贴到设备配置或密钥管理系统，再点击「我已保存」</span>
      </div>

      <el-alert
        v-if="credentialReloadFailed"
        title="已写入新凭证，但 Broker 未重载，旧凭证可能仍然有效"
        :description="`${credentialReloadDetail}。补救：登录 Broker 主机执行重载（systemctl reload mosquitto，或 docker compose restart mqtt），确认成功后设备会改用新口令重连。`"
        type="error"
        :closable="false"
        show-icon
        class="credential-alert"
      />

      <template #footer>
        <el-button type="primary" @click="acknowledgeOneTimePassword">我已保存，关闭</el-button>
      </template>
    </el-dialog>
    <!-- 批量指令（v1.28.0）：逐台结果分类展示；OTA 不在本弹窗，走批量 OTA 跳转 -->
    <el-dialog v-model="batchCommandVisible" title="批量指令" width="760px">
      <el-alert
        v-if="!batchResult"
        :title="`已选择 ${selectedCount} 台设备（一次最多 ${BULK_DEVICE_LIMIT} 台）`"
        type="info"
        :closable="false"
        show-icon
      />

      <template v-if="!batchResult">
        <el-form label-width="110px" class="batch-command-form">
          <el-form-item label="指令类型">
            <el-radio-group v-model="batchCommandType">
              <el-radio-button value="config">运行参数配置</el-radio-button>
              <el-radio-button value="calibrate">传感器校准</el-radio-button>
              <el-radio-button value="reboot">设备重启</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="batchCommandType === 'calibrate'" label="温度偏移">
            <el-input-number v-model="calibrateForm.temperatureOffset" :precision="1" :step="0.1" />
            <span class="section-hint">湿度偏移</span>
            <el-input-number v-model="calibrateForm.humidityOffset" :precision="1" :step="0.1" />
          </el-form-item>
          <el-form-item v-if="batchCommandType === 'config'" label="上报间隔（秒）">
            <el-input-number v-model="configForm.reportIntervalSec" :min="5" :max="3600" />
            <span class="section-hint">休眠窗口（秒）</span>
            <el-input-number v-model="configForm.sleepSec" :min="0" :max="600" />
          </el-form-item>
        </el-form>
        <el-alert
          title="OTA 不支持在本弹窗批量下发：升级需要任务级汇总与逐台进度，请使用「批量 OTA」跳转 OTA 任务页。"
          type="warning"
          :closable="false"
          show-icon
        />
      </template>

      <template v-else>
        <el-alert
          :title="batchResult.replayed ? batchSummary?.title : `批次 ${batchResult.batch_id} 已完成`"
          :type="batchResult.failed.length || batchResult.skipped.length ? 'warning' : 'success'"
          :closable="false"
          show-icon
        />
        <div class="batch-result-groups">
          <div
            v-for="group in batchSummary?.groups || []"
            :key="group.key"
            class="batch-result-group"
            :data-test="`batch-group-${group.key}`"
          >
            <div class="batch-group-title">
              <el-tag
                :type="group.key === 'success' ? 'success' : group.key === 'failed' ? 'danger' : 'info'"
              >
                {{ group.label }} {{ group.count }} 台
              </el-tag>
            </div>
            <el-table v-if="group.items.length" :data="group.items" size="small" row-key="device_id">
              <el-table-column prop="device_id" label="设备 ID" min-width="160" />
              <el-table-column prop="detail" label="结果 / 原因" min-width="260" show-overflow-tooltip />
            </el-table>
            <div v-else class="section-hint">无</div>
          </div>
        </div>
      </template>

      <template #footer>
        <el-button @click="batchCommandVisible = false">
          {{ batchResult ? '关闭' : '取消' }}
        </el-button>
        <el-button
          v-if="!batchResult"
          type="primary"
          :loading="batchSubmitting"
          :disabled="selectedCount === 0"
          @click="handleBatchCommand"
        >
          确认下发（{{ selectedCount }} 台）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.status-select {
  width: 130px;
}

.group-select {
  width: 160px;
}

.filter-spacer {
  flex: 1;
}

.keyword-input {
  width: 240px;
}

.bulk-tip {
  margin-bottom: 12px;
}

.bulk-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.bulk-result {
  margin-top: 12px;
}

.total-hint {
  margin-left: auto;
  font-size: 13px;
  color: #94a3b8;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.device-table :deep(.el-table__row) {
  cursor: pointer;
}

.drawer-body {
  min-height: 400px;
}

.detail-desc {
  margin-bottom: 20px;
}

.drawer-section {
  margin-top: 18px;
}

.section-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}

.section-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #0f172a;
}

.section-hint {
  font-size: 12px;
  color: #94a3b8;
}

.section-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.cmd-type-group {
  margin-bottom: 16px;
}

.readonly-tip {
  margin-bottom: 12px;
}

.credential-alert {
  margin-bottom: 12px;
}

.credential-desc {
  margin-bottom: 8px;
}

.credential-fingerprint-note {
  margin: 0 0 12px;
}

.credential-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.one-time-password {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 14px 0;
  padding: 12px;
  border: 1px solid #fcd34d;
  border-radius: 6px;
  background: #fffbeb;
}

.password-label {
  font-size: 12px;
  color: #92400e;
}

.password-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  word-break: break-all;
  user-select: all;
}

.cmd-form {
  margin-bottom: 16px;
}

.cmd-form :deep(.el-form-item) {
  margin-bottom: 12px;
}

.full-width {
  width: 100%;
}

.batch-command-form {
  margin-top: 12px;
}

.batch-result-groups {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.batch-group-title {
  margin-bottom: 6px;
}

@media (max-width: 768px) {
  .keyword-input {
    width: 100%;
  }

  .total-hint {
    margin-left: 0;
  }
}
</style>
