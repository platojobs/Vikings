<script setup lang="ts">
/**
 * 聚合趋势图：按小时/天展示温度 / 湿度 / 电压均值曲线，
 * 悬浮提示同时给出该时间桶的 min ~ max 区间与样本数。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '@/utils/echarts'
import type { EChartsOption } from 'echarts'
import type { AggregateBucket, AggregatePoint } from '@/types'

const props = withDefaults(
  defineProps<{ points: AggregatePoint[]; bucket: AggregateBucket; height?: string }>(),
  { height: '440px' },
)

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

const pad = (value: number): string => String(value).padStart(2, '0')

/** 按小时聚合显示「月-日 时:分」，按天聚合只显示「月-日」 */
function formatBucketTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  if (props.bucket === 'day') return day
  return `${day} ${pad(date.getHours())}:00`
}

interface PointData {
  value: number
  min: number
  max: number
  count: number
}

function buildOption(points: AggregatePoint[]): EChartsOption {
  const sorted = [...points].sort(
    (a, b) => new Date(a.bucket_start).getTime() - new Date(b.bucket_start).getTime(),
  )
  const times = sorted.map((p) => formatBucketTime(p.bucket_start))

  const toSeries = (
    name: string,
    pick: (p: AggregatePoint) => [number, number, number],
    yAxisIndex: number,
  ) => ({
    name,
    type: 'line' as const,
    smooth: true,
    showSymbol: false,
    yAxisIndex,
    data: sorted.map((p): PointData => {
      const [avg, min, max] = pick(p)
      return { value: Number(avg.toFixed(2)), min, max, count: p.count }
    }),
  })

  return {
    color: ['#2563eb', '#06b6d4', '#f59e0b'],
    tooltip: {
      trigger: 'axis',
      formatter: (params: unknown) => {
        const items = params as Array<{
          seriesName: string
          data: PointData
          marker: string
          axisValue?: string
        }>
        if (!items.length) return ''
        const lines = [`<b>${items[0].axisValue ?? ''}</b>`]
        for (const item of items) {
          lines.push(
            `${item.marker}${item.seriesName}：${item.data.value}（${item.data.min} ~ ${item.data.max}）`,
          )
        }
        lines.push(`样本数：${items[0].data.count}`)
        return lines.join('<br/>')
      },
    },
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
        axisLabel: { color: '#94a3b8' },
        splitLine: { show: false },
      },
    ],
    series: [
      toSeries('温度均值', (p) => [p.temperature_avg, p.temperature_min, p.temperature_max], 0),
      toSeries('湿度均值', (p) => [p.humidity_avg, p.humidity_min, p.humidity_max], 0),
      toSeries('电压均值', (p) => [p.voltage_avg, p.voltage_min, p.voltage_max], 1),
    ],
  }
}

function render(): void {
  chart?.setOption(buildOption(props.points), true)
}

onMounted(() => {
  if (!chartEl.value) return
  chart = echarts.init(chartEl.value)
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(chartEl.value)
})

watch(
  () => [props.points, props.bucket],
  () => render(),
)

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="chartEl" class="aggregate-chart" :style="{ height: props.height }"></div>
</template>

<style scoped>
.aggregate-chart {
  width: 100%;
}
</style>
