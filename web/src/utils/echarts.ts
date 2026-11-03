/**
 * ECharts 按需引入：只注册项目实际用到的组件，避免把整个库（~1MB）打进首屏。
 *
 * 用到的图表能力：
 * - 折线图（温度/湿度/电压曲线）
 * - 网格坐标系（双 Y 轴）
 * - 提示框、图例
 * - 类目轴 + 时间轴
 * - Canvas 渲染器
 */
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

export { echarts }
export type { EChartsOption, ECharts } from 'echarts'
