<script setup lang="ts">
/**
 * 传感器历史折线图：温度 / 湿度 / 电压三条线，双 Y 轴。
 * 容器尺寸变化（如抽屉展开）时通过 ResizeObserver 自动重绘。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '@/utils/echarts'
import type { EChartsOption } from 'echarts'
import type { SensorRecord } from '@/types'
import { formatShortTime } from '@/utils/format'

const props = withDefaults(defineProps<{ records: SensorRecord[]; height?: string }>(), {
  height: '320px',
})

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function buildOption(records: SensorRecord[]): EChartsOption {
  const sorted = [...records].sort(
    (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  )
  const times = sorted.map((item) => formatShortTime(new Date(item.ts).getTime()))

  return {
    color: ['#2563eb', '#06b6d4', '#f59e0b'],
    tooltip: { trigger: 'axis' },
    legend: { top: 0, itemWidth: 14, itemGap: 16 },
    grid: { left: 20, right: 56, top: 40, bottom: 28, containLabel: true },
    xAxis: {
      type: 'category',
      data: times,
      boundaryGap: false,
      axisLabel: { hideOverlap: true, color: '#94a3b8' },
      axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: [
      {
        type: 'value',
        name: '温度(℃) / 湿度(%)',
        nameGap: 16,
        nameTextStyle: { color: '#64748b', fontSize: 12 },
        scale: true,
        axisLabel: { color: '#94a3b8' },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      {
        type: 'value',
        name: '电压(V)',
        nameGap: 16,
        nameTextStyle: { color: '#64748b', fontSize: 12 },
        scale: true,
        axisLabel: { color: '#94a3b8', formatter: '{value}' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '温度',
        type: 'line',
        smooth: true,
        showSymbol: false,
        yAxisIndex: 0,
        data: sorted.map((item) => item.temperature),
      },
      {
        name: '湿度',
        type: 'line',
        smooth: true,
        showSymbol: false,
        yAxisIndex: 0,
        data: sorted.map((item) => item.humidity),
      },
      {
        name: '电压',
        type: 'line',
        smooth: true,
        showSymbol: false,
        yAxisIndex: 1,
        data: sorted.map((item) => item.voltage),
      },
    ],
  }
}

function render(): void {
  chart?.setOption(buildOption(props.records), true)
}

onMounted(() => {
  if (!chartEl.value) return
  chart = echarts.init(chartEl.value)
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(chartEl.value)
})

watch(
  () => props.records,
  () => render(),
)

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="chartEl" class="sensor-chart" :style="{ height: props.height }"></div>
</template>

<style scoped>
.sensor-chart {
  width: 100%;
}
</style>
