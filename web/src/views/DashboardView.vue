<script setup lang="ts">
/**
 * 实时仪表盘：
 * - 顶部 4 个指标卡（来自 /api/stats/overview）
 * - 多设备实时温度曲线：首屏用 /api/records/latest 兜底，之后订阅 MQTT 追加
 * - 右侧实时告警：REST 拉取未确认告警 + 订阅 device/system/alert
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { fetchAlerts } from '@/api/alerts'
import { fetchLatestRecords } from '@/api/devices'
import { fetchStatsOverview } from '@/api/stats'
import { onStatusChange, subscribe } from '@/services/mqtt'
import AlertCard from '@/components/AlertCard.vue'
import { toTimestamp } from '@/utils/format'
import type {
  AlertItem,
  AlertView,
  DeviceStatusMessage,
  SensorReportMessage,
  StatsOverview,
  SystemAlertMessage,
} from '@/types'

/** 每台设备最多保留的实时点位，避免长时间运行内存与渲染压力 */
const POINT_LIMIT = 60

const stats = reactive<StatsOverview>({
  device_total: 0,
  device_online: 0,
  device_offline: 0,
  report_today: 0,
  alert_open: 0,
})
const statsLoading = ref(true)
const alerts = ref<AlertView[]>([])
const alertsLoading = ref(true)
const mqttConnected = ref(false)

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

/** deviceId -> [时间戳, 温度] 点位集合（非响应式，图表更新时统一读取） */
const seriesMap = new Map<string, Array<[number, number]>>()
const offFns: Array<() => void> = []
let statsTimer: ReturnType<typeof setTimeout> | null = null

const metricCards = computed(() => [
  { label: '设备总数', value: stats.device_total, extra: '已接入平台的设备', color: '#2563eb' },
  { label: '在线设备', value: stats.device_online, extra: 'MQTT 长连接在线', color: '#16a34a' },
  { label: '离线设备', value: stats.device_offline, extra: '休眠或异常掉线', color: '#94a3b8' },
  { label: '今日上报', value: stats.report_today, extra: '今日传感器数据条数', color: '#0ea5e9' },
])

function pushPoint(deviceId: string, ts: number, temperature: number): void {
  let points = seriesMap.get(deviceId)
  if (!points) {
    points = []
    seriesMap.set(deviceId, points)
  }
  const last = points[points.length - 1]
  if (last && last[0] === ts) {
    last[1] = temperature
    return
  }
  points.push([ts, temperature])
  while (points.length > POINT_LIMIT) points.shift()
}

function buildOption(): EChartsOption {
  const deviceIds = Array.from(seriesMap.keys()).sort()
  return {
    color: ['#2563eb', '#06b6d4', '#f59e0b', '#8b5cf6', '#10b981', '#ef4444', '#0ea5e9', '#f97316'],
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 0, itemWidth: 14, itemGap: 16 },
    grid: { left: 12, right: 20, top: 40, bottom: 20, containLabel: true },
    xAxis: {
      type: 'time',
      axisLabel: { hideOverlap: true, color: '#94a3b8' },
      axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: {
      type: 'value',
      name: '温度 (℃)',
      scale: true,
      nameTextStyle: { color: '#64748b', fontSize: 12 },
      axisLabel: { color: '#94a3b8' },
      splitLine: { lineStyle: { color: '#f1f5f9' } },
    },
    series: deviceIds.map((deviceId) => ({
      name: deviceId,
      type: 'line',
      smooth: true,
      showSymbol: true,
      symbolSize: 4,
      data: seriesMap.get(deviceId) ?? [],
      emphasis: { focus: 'series' },
    })),
    animation: false,
  }
}

function renderChart(): void {
  chart?.setOption(buildOption(), { notMerge: true })
}

async function loadStats(): Promise<void> {
  try {
    Object.assign(stats, await fetchStatsOverview())
  } catch {
    // 错误已由 HTTP 层统一提示
  } finally {
    statsLoading.value = false
  }
}

async function loadLatest(): Promise<void> {
  try {
    const rows = await fetchLatestRecords()
    rows.forEach((row) => pushPoint(row.device_id, toTimestamp(row.ts), row.temperature))
    renderChart()
  } catch {
    // 首屏兜底失败时保持空图，等待 MQTT 实时数据
  }
}

function toAlertView(item: AlertItem): AlertView {
  return {
    id: item.alert_id,
    deviceId: item.device_id,
    level: item.level,
    metric: item.metric,
    value: item.value,
    threshold: item.threshold,
    message: item.message,
    aiGenerated: item.ai_generated,
    ts: toTimestamp(item.created_at),
  }
}

async function loadAlerts(): Promise<void> {
  try {
    const page = await fetchAlerts({ status: 'open', page: 1, pageSize: 10 })
    alerts.value = page.items.map(toAlertView)
  } catch {
    // 忽略，实时告警仍会通过 MQTT 到达
  } finally {
    alertsLoading.value = false
  }
}

/** 上报 / 上下线只刷新统计，做 1.5 秒节流避免打爆网关 */
function scheduleStatsRefresh(): void {
  if (statsTimer) return
  statsTimer = setTimeout(() => {
    statsTimer = null
    loadStats()
  }, 1500)
}

function handleReport(payload: unknown): void {
  const msg = payload as SensorReportMessage
  if (!msg?.deviceId) return
  pushPoint(msg.deviceId, Number(msg.ts) || Date.now(), Number(msg.temperature))
  renderChart()
  scheduleStatsRefresh()
}

function handleStatus(payload: unknown): void {
  const msg = payload as DeviceStatusMessage
  if (!msg?.deviceId) return
  scheduleStatsRefresh()
}

function handleAlert(payload: unknown): void {
  const msg = payload as SystemAlertMessage
  if (!msg?.alertId) return
  const view: AlertView = {
    id: msg.alertId,
    deviceId: msg.deviceId,
    deviceName: msg.deviceName,
    level: msg.level,
    metric: msg.metric,
    value: Number(msg.value),
    threshold: Number(msg.threshold),
    message: msg.message,
    aiGenerated: Boolean(msg.aiGenerated),
    ts: Number(msg.ts) || Date.now(),
  }
  alerts.value = [view, ...alerts.value.filter((item) => item.id !== view.id)].slice(0, 20)
}

onMounted(async () => {
  if (chartEl.value) {
    chart = echarts.init(chartEl.value)
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(chartEl.value)
  }

  offFns.push(
    onStatusChange((connected) => {
      mqttConnected.value = connected
    }),
  )
  offFns.push(subscribe('device/+/sensor/report', handleReport))
  offFns.push(subscribe('device/+/status', handleStatus))
  offFns.push(subscribe('device/system/alert', handleAlert))

  await Promise.all([loadStats(), loadLatest(), loadAlerts()])
  renderChart()
})

onBeforeUnmount(() => {
  offFns.forEach((off) => off())
  if (statsTimer) clearTimeout(statsTimer)
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="page dashboard-page">
    <el-row :gutter="16" class="metric-row" v-loading="statsLoading">
      <el-col v-for="card in metricCards" :key="card.label" :xs="12" :sm="12" :md="6">
        <div class="metric-card" :style="{ borderTopColor: card.color }">
          <div class="metric-label">{{ card.label }}</div>
          <div class="metric-value" :style="{ color: card.color }">{{ card.value }}</div>
          <div class="metric-extra">{{ card.extra }}</div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="content-row">
      <el-col :xs="24" :lg="16">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="card-header">
              <span class="card-title">实时温度曲线</span>
              <div class="header-meta">
                <el-tag :type="mqttConnected ? 'success' : 'info'" size="small" effect="plain">
                  {{ mqttConnected ? 'MQTT 实时推送' : 'MQTT 重连中' }}
                </el-tag>
                <span class="header-hint">订阅 device/+/sensor/report</span>
              </div>
            </div>
          </template>
          <div ref="chartEl" class="realtime-chart"></div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="8">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="card-header">
              <span class="card-title">实时告警</span>
              <span class="alert-count">未确认 {{ stats.alert_open }} 条</span>
            </div>
          </template>
          <div v-loading="alertsLoading" class="alert-list">
            <AlertCard v-for="alert in alerts" :key="alert.id" :alert="alert" />
            <el-empty
              v-if="!alerts.length && !alertsLoading"
              description="暂无告警，设备运行正常"
              :image-size="80"
            />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.metric-row {
  row-gap: 16px;
}

.realtime-chart {
  width: 100%;
  height: 420px;
}

.panel-card {
  height: 100%;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.header-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-hint {
  font-size: 12px;
  color: #94a3b8;
}

.alert-count {
  font-size: 13px;
  color: #ef4444;
}

.alert-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 420px;
  min-height: 120px;
  overflow-y: auto;
  padding-right: 4px;
}

@media (max-width: 1199px) {
  .content-row {
    row-gap: 16px;
  }
}
</style>
