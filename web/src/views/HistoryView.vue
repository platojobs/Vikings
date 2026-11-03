<script setup lang="ts">
/**
 * 历史查询：选择设备 + 本地时间范围，调用 /api/devices/{id}/records。
 * 时间以毫秒时间戳传给网关，避免字符串时区歧义。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchDeviceRecords, fetchDevices } from '@/api/devices'
import SensorChart from '@/components/SensorChart.vue'
import { formatDateTime } from '@/utils/format'
import type { Device, SensorRecord } from '@/types'

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
const records = ref<SensorRecord[]>([])
const loading = ref(false)
const devicesLoading = ref(false)

const rangeLabel = computed(() => {
  if (!records.value.length) return ''
  const first = records.value[0]
  const last = records.value[records.value.length - 1]
  return `${formatDateTime(first.ts)} ~ ${formatDateTime(last.ts)}`
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
    records.value = await fetchDeviceRecords(deviceId.value, {
      start: start.getTime(),
      end: end.getTime(),
      limit: 1000,
    })
  } catch {
    records.value = []
  } finally {
    loading.value = false
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

        <el-button type="primary" :loading="loading" @click="queryRecords">查询</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">温度 / 湿度 / 电压历史曲线</span>
          <span v-if="rangeLabel" class="range-hint">
            共 {{ records.length }} 条 · {{ rangeLabel }}
          </span>
        </div>
      </template>

      <div v-loading="loading" class="chart-wrap">
        <SensorChart v-if="records.length" :records="records" height="440px" />
        <el-empty
          v-else
          description="暂无数据，请调整设备或时间范围后重新查询"
          :image-size="100"
        />
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
