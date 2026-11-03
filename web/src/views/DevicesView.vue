<script setup lang="ts">
/**
 * 设备列表：状态筛选 + 关键字搜索 + 分页。
 * 点击任意行打开抽屉，展示详情、历史数据折线图与指令历史。
 */
import { onMounted, reactive, ref } from 'vue'
import {
  fetchDeviceCommands,
  fetchDeviceDetail,
  fetchDeviceRecords,
  fetchDevices,
} from '@/api/devices'
import { formatDateTime, formatMetric } from '@/utils/format'
import { commandStateLabel, commandStateTagType } from '@/utils/labels'
import DeviceStatusTag from '@/components/DeviceStatusTag.vue'
import SensorChart from '@/components/SensorChart.vue'
import type { CommandItem, Device, DeviceDetail, DeviceStatus, SensorRecord } from '@/types'

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

onMounted(loadDevices)
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
            <h3>历史数据</h3>
            <span class="section-hint">最近 {{ historyRecords.length }} 条</span>
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

@media (max-width: 768px) {
  .keyword-input {
    width: 100%;
  }

  .total-hint {
    margin-left: 0;
  }
}
</style>
