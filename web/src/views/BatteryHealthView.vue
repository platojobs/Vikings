<script setup lang="ts">
/**
 * 电池健康与续航看板（v1.32.0）。
 *
 * 定位：在「电压阈值告警」之外，提前看清哪些设备会在下一次巡检前掉电。
 * - 数据全部来自 GET /api/stats/battery-health，口径由网关统一裁决；
 * - 风险 = 当前电量 + 衰减速度推算的剩余天数，二者任一触碰阈值即升级；
 * - 只读页面（viewer / operator / admin 一致），不在这里做任何写操作。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { echarts } from '@/utils/echarts'
import type { EChartsOption } from 'echarts'
import { fetchBatteryHealth } from '@/api/battery'
import { fetchDeviceGroups } from '@/api/devices'
import { formatDateTime, formatMetric } from '@/utils/format'
import type { BatteryHealth, BatteryRisk, DeviceGroupStat } from '@/types'

const loading = ref(false)
const error = ref('')
const data = ref<BatteryHealth | null>(null)
const groupOptions = ref<DeviceGroupStat[]>([])

const filters = reactive({
  group: '',
  days: 7,
})

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

const riskLabels: Record<BatteryRisk, string> = {
  critical: '严重',
  warning: '预警',
  healthy: '健康',
  unknown: '未知',
}

function riskTagType(risk: BatteryRisk): 'danger' | 'warning' | 'success' | 'info' {
  if (risk === 'critical') return 'danger'
  if (risk === 'warning') return 'warning'
  if (risk === 'healthy') return 'success'
  return 'info'
}

function batteryStatus(risk: BatteryRisk): 'success' | 'warning' | 'exception' | undefined {
  if (risk === 'critical') return 'exception'
  if (risk === 'warning') return 'warning'
  if (risk === 'healthy') return 'success'
  return undefined
}

const summaryCards = computed(() => {
  const value = data.value
  if (!value) return []
  return [
    {
      label: '设备总数',
      value: value.device_total,
      extra: `窗口内 ${value.battery_known} 台有电量数据`,
      color: '#2563eb',
    },
    {
      label: '平均电量',
      value: value.avg_battery === null ? '—' : `${value.avg_battery}%`,
      extra: '窗口内最新电量均值',
      color: '#0ea5e9',
    },
    {
      label: '严重',
      value: value.critical,
      extra: `电量 ≤ ${value.critical_percent}% 或预计 ≤ 7 天`,
      color: '#dc2626',
    },
    {
      label: '预警',
      value: value.warning,
      extra: `电量 ≤ ${value.warning_percent}% 或预计 ≤ 30 天`,
      color: '#f59e0b',
    },
    {
      label: '未知',
      value: value.unknown,
      extra: `窗口 ${value.window_days} 天内无上报`,
      color: '#94a3b8',
    },
  ]
})

const windowText = computed(() => {
  if (!data.value) return ''
  return `统计窗口 ${data.value.window_days} 天（自 ${formatDateTime(data.value.window_start)} 起）`
})

const truncated = computed(() => {
  const value = data.value
  if (!value) return ''
  if (value.watchlist_total <= value.watchlist.length) return ''
  return `共 ${value.watchlist_total} 台设备处于风险状态，按紧急程度优先展示前 ${value.watchlist.length} 台`
})

function buildOption(): EChartsOption {
  const points = data.value?.trend ?? []
  return {
    color: ['#2563eb'],
    tooltip: {
      trigger: 'axis',
      formatter: (params: unknown) => {
        const item = (params as Array<{ axisValue: string; value: number; dataIndex: number }>)[0]
        const point = points[item.dataIndex]
        return `${item.axisValue}<br/>平均电量 ${item.value}%<br/>参与设备 ${point?.samples ?? 0} 台`
      },
    },
    grid: { left: 12, right: 24, top: 30, bottom: 20, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: points.map((point) => point.day_start.slice(5, 10)),
      axisLabel: { color: '#94a3b8' },
      axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: {
      type: 'value',
      name: '平均电量 (%)',
      min: 0,
      max: 100,
      nameTextStyle: { color: '#64748b', fontSize: 12 },
      axisLabel: { color: '#94a3b8' },
      splitLine: { lineStyle: { color: '#f1f5f9' } },
    },
    series: [
      {
        name: '平均电量',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: points.map((point) => point.avg_battery),
        areaStyle: { opacity: 0.08 },
      },
    ],
    animation: false,
  }
}

function renderChart(): void {
  if (!chartEl.value) return
  chart?.setOption(buildOption(), { notMerge: true })
}

/** 图表容器随数据一起挂载：数据到位后再初始化，避免在 v-if 未渲染时拿到 null */
async function ensureChart(): Promise<void> {
  await nextTick()
  if (!chartEl.value) {
    chart?.clear()
    return
  }
  if (!chart) {
    chart = echarts.init(chartEl.value)
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(chartEl.value)
  }
  renderChart()
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    data.value = await fetchBatteryHealth({
      group: filters.group || undefined,
      days: filters.days,
    })
    await ensureChart()
  } catch {
    data.value = null
    error.value = '电池健康数据加载失败，请确认网关可用'
    chart?.clear()
  } finally {
    loading.value = false
  }
}

async function loadGroups(): Promise<void> {
  try {
    groupOptions.value = await fetchDeviceGroups()
  } catch {
    // 分组下拉失败不阻塞主数据；筛选仍可手动清空
    groupOptions.value = []
  }
}

function applyFilters(): void {
  load()
}

onMounted(() => {
  loadGroups()
  load()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="page">
    <el-card shadow="never" class="filter-card">
      <div class="section-head">
        <div>
          <span class="section-title">电池健康与续航</span>
          <span class="section-desc">
            风险分层同时看当前电量与衰减速度；窗口内无上报的设备记为「未知」，不补假值
          </span>
        </div>
        <div class="filters">
          <el-select
            v-model="filters.group"
            size="small"
            placeholder="全部分组"
            clearable
            class="group-select"
            @change="applyFilters"
          >
            <el-option
              v-for="group in groupOptions"
              :key="group.group_name"
              :label="`${group.group_name}（${group.total}）`"
              :value="group.group_name"
            />
          </el-select>
          <el-select v-model="filters.days" size="small" class="days-select" @change="applyFilters">
            <el-option :label="'近 7 天'" :value="7" />
            <el-option :label="'近 14 天'" :value="14" />
            <el-option :label="'近 30 天'" :value="30" />
          </el-select>
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
      <div class="window-text">{{ windowText }}</div>
    </el-card>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="page-error" />

    <div v-loading="loading" class="content">
      <el-empty v-if="!loading && data && data.device_total === 0" description="暂无设备数据" />

      <template v-else-if="data">
        <div class="summary-grid">
          <el-card v-for="card in summaryCards" :key="card.label" shadow="never" class="summary-card">
            <div class="summary-label">{{ card.label }}</div>
            <div class="summary-value" :style="{ color: card.color }">{{ card.value }}</div>
            <div class="summary-extra">{{ card.extra }}</div>
          </el-card>
        </div>

        <el-card shadow="never" class="chart-card">
          <div class="card-title">整机队平均电量趋势</div>
          <el-empty v-if="data.trend.length === 0" description="窗口内暂无电量上报" :image-size="60" />
          <div v-else ref="chartEl" class="chart" />
        </el-card>

        <el-card shadow="never" class="table-card">
          <div class="card-title">续航风险清单</div>
          <div v-if="truncated" class="truncated-hint">{{ truncated }}</div>
          <el-empty
            v-if="data.watchlist.length === 0"
            description="当前没有处于预警或严重状态的设备"
            :image-size="60"
          />
          <el-table v-else :data="data.watchlist" size="small" row-key="device_id">
            <el-table-column label="设备" min-width="170">
              <template #default="{ row }">
                <div class="device-name">{{ row.name }}</div>
                <div class="device-id">{{ row.device_id }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="group_name" label="分组" min-width="100" />
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'online' ? 'success' : 'info'" size="small" effect="plain">
                  {{ row.status === 'online' ? '在线' : '离线' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="电量" min-width="150">
              <template #default="{ row }">
                <el-progress
                  v-if="row.battery !== null"
                  :percentage="row.battery"
                  :status="batteryStatus(row.risk)"
                  :stroke-width="8"
                />
                <span v-else class="muted">无数据</span>
              </template>
            </el-table-column>
            <el-table-column label="电压" width="90">
              <template #default="{ row }">
                {{ row.voltage === null ? '—' : `${formatMetric(row.voltage, 2)} V` }}
              </template>
            </el-table-column>
            <el-table-column label="衰减" width="110">
              <template #default="{ row }">
                {{ row.drain_per_day === null ? '稳定' : `${formatMetric(row.drain_per_day, 2)} %/天` }}
              </template>
            </el-table-column>
            <el-table-column label="预计到 15%" width="105">
              <template #default="{ row }">
                {{ row.days_to_critical === null ? '—' : `${formatMetric(row.days_to_critical, 1)} 天` }}
              </template>
            </el-table-column>
            <el-table-column label="未处理告警" width="100" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.open_alert_count > 0" type="warning" size="small" effect="plain">
                  {{ row.open_alert_count }}
                </el-tag>
                <span v-else class="muted">0</span>
              </template>
            </el-table-column>
            <el-table-column label="最近上报" width="160">
              <template #default="{ row }">{{ formatDateTime(row.last_report_at) }}</template>
            </el-table-column>
            <el-table-column label="风险" width="80" fixed="right">
              <template #default="{ row }">
                <el-tag :type="riskTagType(row.risk)" size="small">{{ riskLabels[row.risk as BatteryRisk] }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="table-card">
          <div class="card-title">按分组汇总</div>
          <el-table :data="data.groups" size="small" row-key="group_name">
            <el-table-column prop="group_name" label="分组" min-width="140" />
            <el-table-column prop="total" label="设备数" width="90" align="center" />
            <el-table-column prop="critical" label="严重" width="80" align="center" />
            <el-table-column prop="warning" label="预警" width="80" align="center" />
            <el-table-column prop="healthy" label="健康" width="80" align="center" />
            <el-table-column prop="unknown" label="未知" width="80" align="center" />
            <el-table-column label="平均电量" width="110" align="center">
              <template #default="{ row }">
                {{ row.avg_battery === null ? '—' : `${row.avg_battery}%` }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.filter-card :deep(.el-card__body) {
  padding: 14px 18px;
}

.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: #0f172a;
}

.section-desc {
  margin-left: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.filters {
  display: flex;
  align-items: center;
  gap: 8px;
}

.group-select {
  width: 170px;
}

.days-select {
  width: 110px;
}

.window-text {
  margin-top: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.page-error {
  margin-bottom: 4px;
}

.content {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 120px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

.summary-card :deep(.el-card__body) {
  padding: 14px 16px;
}

.summary-label {
  font-size: 12px;
  color: #64748b;
}

.summary-value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
}

.summary-extra {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
}

.chart-card :deep(.el-card__body),
.table-card :deep(.el-card__body) {
  padding: 14px 18px 18px;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: #0f172a;
  margin-bottom: 10px;
}

.chart {
  height: 280px;
}

.truncated-hint {
  margin: -4px 0 10px;
  font-size: 12px;
  color: #b45309;
}

.device-name {
  font-size: 13px;
  color: #0f172a;
}

.device-id {
  font-size: 12px;
  color: #94a3b8;
}

.muted {
  color: #94a3b8;
}
</style>
