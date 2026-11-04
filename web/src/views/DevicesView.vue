<script setup lang="ts">
/**
 * 设备列表：状态筛选 + 关键字搜索 + 分页。
 * 点击任意行打开抽屉，展示详情、历史数据折线图与指令历史。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  fetchDeviceCommands,
  fetchDeviceDetail,
  fetchDeviceRecords,
  fetchDevices,
  sendCommand,
} from '@/api/devices'
import { formatDateTime, formatMetric } from '@/utils/format'
import { commandStateLabel, commandStateTagType } from '@/utils/labels'
import DeviceStatusTag from '@/components/DeviceStatusTag.vue'
import SensorChart from '@/components/SensorChart.vue'
import { subscribe } from '@/services/mqtt'
import { canOperate } from '@/utils/permissions'
import type {
  CommandAckMessage,
  CommandItem,
  CommandType,
  Device,
  DeviceDetail,
  DeviceStatus,
  SensorRecord,
} from '@/types'

const query = reactive<{ status: DeviceStatus | ''; keyword: string }>({
  status: '',
  keyword: '',
})

const devices = ref<Device[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

async function loadDevices(): Promise<void> {
  loading.value = true
  try {
    const result = await fetchDevices({
      status: query.status || undefined,
      keyword: query.keyword.trim() || undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    devices.value = result.items
    total.value = result.total
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
  page.value = 1
  loadDevices()
}

function handleSizeChange(): void {
  page.value = 1
  loadDevices()
}

// ==================== 详情抽屉 ====================

const drawerVisible = ref(false)
const drawerLoading = ref(false)
const detail = ref<DeviceDetail | null>(null)
const historyRecords = ref<SensorRecord[]>([])
const commands = ref<CommandItem[]>([])

async function openDevice(row: Device): Promise<void> {
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

// ==================== 指令下发面板 ====================

/** 只读账号隐藏下发能力，与后端 require_role(operator) 保持一致 */
const canWrite = computed(() => canOperate())

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

onMounted(loadDevices)

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
      </div>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="devices"
        row-key="device_id"
        class="device-table"
        @row-click="openDevice"
      >
        <el-table-column prop="device_id" label="设备 ID" min-width="120" />
        <el-table-column prop="name" label="设备名称" min-width="160" />
        <el-table-column prop="model" label="型号" min-width="110" />
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
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDevice(row)">详情</el-button>
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
  width: 150px;
}

.keyword-input {
  width: 280px;
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

.cmd-form {
  margin-bottom: 16px;
}

.cmd-form :deep(.el-form-item) {
  margin-bottom: 12px;
}

.full-width {
  width: 100%;
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
