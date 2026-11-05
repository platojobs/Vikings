<script setup lang="ts">
/**
 * 历史查询：选择设备 + 本地时间范围，支持明细与按小时/按天聚合两种视图。
 * 时间以毫秒时间戳传给网关，避免字符串时区歧义。
 * v1.30.0：新增「导出 CSV」，由服务端流式生成，前端只负责下载与进度展示。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchDeviceAggregate, fetchDeviceRecords, fetchDevices } from '@/api/devices'
import { exportRecords, formatBytes } from '@/api/exports'
import SensorChart from '@/components/SensorChart.vue'
import AggregateChart from '@/components/AggregateChart.vue'
import { formatDateTime } from '@/utils/format'
import { canOperate } from '@/utils/permissions'
import type { AggregateBucket, AggregatePoint, Device, SensorRecord } from '@/types'

/** 视图模式：detail=明细，hour/day=聚合粒度 */
type ViewMode = 'detail' | AggregateBucket

/** 默认最近 6 小时 */
function defaultRange(): [Date, Date] {
  const end = new Date()
  return [new Date(end.getTime() - 6 * 60 * 60 * 1000), end]
}

const shortcuts = [
  {
    text: '最近 1 小时',
    value: (): [Date, Date] => [new Date(Date.now() - 60 * 60 * 1000), new Date()],
  },
  {
    text: '最近 6 小时',
    value: (): [Date, Date] => [new Date(Date.now() - 6 * 60 * 60 * 1000), new Date()],
  },
  {
    text: '最近 24 小时',
    value: (): [Date, Date] => [new Date(Date.now() - 24 * 60 * 60 * 1000), new Date()],
  },
  {
    text: '最近 7 天',
    value: (): [Date, Date] => [new Date(Date.now() - 7 * 24 * 60 * 60 * 1000), new Date()],
  },
]

const devices = ref<Device[]>([])
const deviceId = ref('')
const range = ref<[Date, Date]>(defaultRange())
const mode = ref<ViewMode>('detail')
const records = ref<SensorRecord[]>([])
const points = ref<AggregatePoint[]>([])
const loading = ref(false)
const devicesLoading = ref(false)
/** 导出与查询共用筛选条件；viewer 只读，不提供批量外带数据的入口 */
const canExport = computed(() => canOperate())
const exporting = ref(false)
const exportLoaded = ref(0)

const rangeLabel = computed(() => {
  if (mode.value === 'detail') {
    if (!records.value.length) return ''
    const first = records.value[0]
    const last = records.value[records.value.length - 1]
    return `共 ${records.value.length} 条 · ${formatDateTime(first.ts)} ~ ${formatDateTime(last.ts)}`
  }
  if (!points.value.length) return ''
  return `共 ${points.value.length} 个时间桶（按${mode.value === 'hour' ? '小时' : '天'}聚合）`
})

async function queryRecords(): Promise<void> {
  if (!deviceId.value) {
    ElMessage.warning('请先选择设备')
    return
  }
  const [start, end] = range.value
  if (!start || !end) {
    ElMessage.warning('请选择时间范围')
    return
  }
  if (end.getTime() <= start.getTime()) {
    ElMessage.warning('结束时间需晚于开始时间')
    return
  }

  loading.value = true
  try {
    if (mode.value === 'detail') {
      records.value = await fetchDeviceRecords(deviceId.value, {
        start: start.getTime(),
        end: end.getTime(),
        limit: 1000,
      })
    } else {
      points.value = await fetchDeviceAggregate(deviceId.value, mode.value, {
        start: start.getTime(),
        end: end.getTime(),
      })
    }
  } catch {
    records.value = []
    points.value = []
  } finally {
    loading.value = false
  }
}

/** 切换视图模式后重新查询 */
function handleModeChange(): void {
  if (deviceId.value) queryRecords()
}

/** 当前筛选是否可用于导出：与查询使用同一套校验，避免导出一份「看起来对」的空文件 */
function validateExportScope(): [number, number] | null {
  if (!deviceId.value) {
    ElMessage.warning('请先选择设备')
    return null
  }
  const [start, end] = range.value
  if (!start || !end) {
    ElMessage.warning('请选择时间范围')
    return null
  }
  if (end.getTime() <= start.getTime()) {
    ElMessage.warning('结束时间需晚于开始时间')
    return null
  }
  return [start.getTime(), end.getTime()]
}

async function handleExport(): Promise<void> {
  const scope = validateExportScope()
  if (!scope) return
  exporting.value = true
  exportLoaded.value = 0
  try {
    const result = await exportRecords(
      { deviceId: deviceId.value, start: scope[0], end: scope[1] },
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

async function loadDevices(): Promise<void> {
  devicesLoading.value = true
  try {
    const result = await fetchDevices({ page: 1, pageSize: 200 })
    devices.value = result.items
    if (!deviceId.value && result.items.length > 0) {
      deviceId.value = result.items[0].device_id
      await queryRecords()
    }
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    devicesLoading.value = false
  }
}

onMounted(loadDevices)
</script>

<template>
  <div class="page history-page">
    <el-card shadow="never">
      <div class="filter-bar">
        <span class="filter-label">设备</span>
        <el-select
          v-model="deviceId"
          class="device-select"
          placeholder="请选择设备"
          filterable
          :loading="devicesLoading"
          @change="queryRecords"
        >
          <el-option
            v-for="device in devices"
            :key="device.device_id"
            :label="`${device.name}（${device.device_id}）`"
            :value="device.device_id"
          />
        </el-select>

        <span class="filter-label">时间范围</span>
        <el-date-picker
          v-model="range"
          class="range-picker"
          type="datetimerange"
          :shortcuts="shortcuts"
          :clearable="false"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
        />

        <span class="filter-label">视图</span>
        <el-radio-group v-model="mode" @change="handleModeChange">
          <el-radio-button value="detail">明细</el-radio-button>
          <el-radio-button value="hour">按小时</el-radio-button>
          <el-radio-button value="day">按天</el-radio-button>
        </el-radio-group>

        <el-button type="primary" :loading="loading" @click="queryRecords">查询</el-button>
        <el-button
          :loading="exporting"
          :disabled="!canExport"
          :title="canExport ? '' : '只读账号不可导出'"
          @click="handleExport"
        >
          {{ exporting ? `导出中 ${formatBytes(exportLoaded)}` : '导出 CSV' }}
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">
            {{ mode === 'detail' ? '温度 / 湿度 / 电压历史曲线' : '温度 / 湿度 / 电压聚合趋势（均值）' }}
          </span>
          <span v-if="rangeLabel" class="range-hint">{{ rangeLabel }}</span>
        </div>
      </template>

      <div v-loading="loading" class="chart-wrap">
        <template v-if="mode === 'detail'">
          <SensorChart v-if="records.length" :records="records" height="440px" />
          <el-empty
            v-else
            description="暂无数据，请调整设备或时间范围后重新查询"
            :image-size="100"
          />
        </template>
        <template v-else>
          <AggregateChart
            v-if="points.length"
            :points="points"
            :bucket="mode"
            height="440px"
          />
          <el-empty
            v-else
            description="该时间范围内没有聚合数据，可尝试扩大范围"
            :image-size="100"
          />
        </template>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.filter-label {
  font-size: 13px;
  color: #64748b;
}

.device-select {
  width: 280px;
}

.range-picker {
  width: 380px;
}

.card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.range-hint {
  font-size: 12px;
  color: #94a3b8;
}

.chart-wrap {
  min-height: 440px;
}

@media (max-width: 768px) {
  .device-select,
  .range-picker {
    width: 100%;
  }
}
</style>
