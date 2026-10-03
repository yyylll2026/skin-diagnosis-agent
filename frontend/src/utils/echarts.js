/**
 * ECharts 按需引入。
 *
 * 不用 `import * as echarts from 'echarts'` 全量包——那会把地图、关系图、
 * 各种坐标系全打进产物（gzip 后仍有几百 KB），而本项目只画三种基础图。
 * 这里只注册真正用到的图表与组件，产物体积差一个数量级。
 *
 * 集中在一个模块里注册还有个好处：`echarts.use()` 是全局注册，
 * 分散在多个组件里重复调用既冗余又容易漏（漏注册的表现是运行时
 * 报 "Series xxx is not exists"，而不是构建期报错，很难提前发现）。
 */

import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
])

/** 主题色：与全局 CSS 变量保持一致的青蓝主色系 */
export const CHART_COLORS = {
  primary: '#0e7490',
  primarySoft: 'rgba(14, 116, 144, 0.16)',
  axis: '#94a3b8',
  grid: 'rgba(148, 163, 184, 0.22)',
  text: '#475569',
  textStrong: '#1e293b',
}

/** 自述程度 → 颜色。由重到轻，和「涨红跌绿」无关，这里只是程度语义。 */
export const SEVERITY_COLORS = {
  severe: '#dc2626',
  moderate: '#f59e0b',
  mild: '#0d9488',
  unknown: '#94a3b8',
}

export default echarts
