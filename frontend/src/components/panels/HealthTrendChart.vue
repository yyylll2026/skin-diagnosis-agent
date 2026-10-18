<script setup>
/**
 * 病程趋势面板：把健康记录画成三张图。
 *
 * 「趋势折线 / 部位分布 / 程度分布」三张图回答三个不同问题：
 * - 折线：**我的情况是在变好还是变坏？**（时间维度）
 * - 部位条形：**问题集中在哪几个地方？**（空间维度）
 * - 程度环形：**整体以什么程度为主？**（构成维度）
 *
 * 数据全部由后端 `/health-records/trends` 聚合好，这里不做任何统计计算，
 * 只把结构化结果映射成 option。分数是用户自述程度的编码（0~3），不是临床评分，
 * 所以界面上一律不出现「评分」「等级」这类容易被误读成医学结论的措辞。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getHealthTrends,
  getPatientTrends,
} from '../../api'
import EChart from '../common/EChart.vue'
import { CHART_COLORS, SEVERITY_COLORS } from '../../utils/echarts'

const props = defineProps({
  /** 父组件在增删改记录后自增，用来触发重新拉取 */
  refreshKey: { type: Number, default: 0 },
  /** 传入时走医生工作台接口（GET /health-records/patients/{id}/trends），
   *  不传则聚合当前登录人自己的记录 */
  patientId: { type: String, default: '' },
})

const DAY_OPTIONS = [
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 },
  { label: '近半年', value: 180 },
  { label: '近一年', value: 365 },
]

const SEVERITY_LABELS = {
  unknown: '未标记',
  mild: '轻微',
  moderate: '中等',
  severe: '较明显',
}

/** 分数 → 档位，用于给 Y 轴贴标签 */
const SCORE_LEVELS = { 0: 'unknown', 1: 'mild', 2: 'moderate', 3: 'severe' }

const days = ref(90)
const loading = ref(false)
const trend = ref(null)

async function load() {
  loading.value = true
  try {
    trend.value = props.patientId
      ? await getPatientTrends(props.patientId, days.value)
      : await getHealthTrends(days.value)
  } catch (err) {
    ElMessage.error(err?.message || '加载病程趋势失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.refreshKey, load)
watch(days, load)

const hasData = computed(() => (trend.value?.total_records || 0) > 0)
const hasEarlier = computed(
  () => (trend.value?.total_records_all || 0) > (trend.value?.total_records || 0)
)

/* ------------------------------ 图表配置 ------------------------------ */

const trendOption = computed(() => {
  const daily = trend.value?.daily || []
  return {
    grid: { left: 8, right: 14, top: 34, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const point = daily[params?.[0]?.dataIndex ?? -1]
        if (!point) return ''
        if (!point.count) return `${point.recorded_on}<br/>当天没有记录`
        return [
          point.recorded_on,
          `记录 ${point.count} 条`,
          `最明显：${SEVERITY_LABELS[point.worst_severity] || '未标记'}（${point.max_score}）`,
          `平均：${point.avg_score}`,
        ].join('<br/>')
      },
    },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: CHART_COLORS.text, fontSize: 11 },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: daily.map((point) => point.recorded_on),
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisTick: { show: false },
      axisLabel: {
        color: CHART_COLORS.axis,
        fontSize: 11,
        formatter: (value) => String(value).slice(5).replace('-', '/'),
      },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 3,
      interval: 1,
      splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: {
        color: CHART_COLORS.axis,
        fontSize: 11,
        formatter: (value) => SEVERITY_LABELS[SCORE_LEVELS[value]] || '',
      },
    },
    series: [
      {
        name: '最明显程度',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        connectNulls: false,
        data: daily.map((point) => point.max_score),
        itemStyle: { color: CHART_COLORS.primary },
        lineStyle: { width: 2, color: CHART_COLORS.primary },
        areaStyle: { color: CHART_COLORS.primarySoft },
      },
      {
        name: '平均程度',
        type: 'line',
        smooth: true,
        symbolSize: 4,
        connectNulls: false,
        data: daily.map((point) => point.avg_score),
        itemStyle: { color: '#f59e0b' },
        lineStyle: { width: 1.5, type: 'dashed', color: '#f59e0b' },
      },
    ],
  }
})

const siteOption = computed(() => {
  const top = (trend.value?.body_sites || []).slice(0, 8)
  const labels = top.map((item) => item.body_site).reverse()
  const values = top.map((item) => item.count).reverse()
  return {
    grid: { left: 8, right: 28, top: 6, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const index = labels.length - 1 - (params?.[0]?.dataIndex ?? -1)
        const item = top[index]
        if (!item) return ''
        return [
          item.body_site,
          `记录 ${item.count} 次（占 ${Math.round(item.share * 100)}%）`,
          `最近一次：${item.last_recorded_on}`,
          `最明显：${SEVERITY_LABELS[item.worst_severity] || '未标记'}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { color: CHART_COLORS.axis, fontSize: 11 },
    },
    yAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisTick: { show: false },
      axisLabel: { color: CHART_COLORS.text, fontSize: 11, width: 84, overflow: 'truncate' },
    },
    series: [
      {
        type: 'bar',
        data: values,
        barMaxWidth: 16,
        itemStyle: { color: CHART_COLORS.primary, borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: CHART_COLORS.text, fontSize: 11 },
      },
    ],
  }
})

const severityOption = computed(() => {
  const items = trend.value?.severities || []
  return {
    tooltip: {
      trigger: 'item',
      formatter: (params) => `${params.name}：${params.value} 次（${params.percent}%）`,
    },
    legend: {
      bottom: 0,
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: CHART_COLORS.text, fontSize: 11 },
    },
    series: [
      {
        type: 'pie',
        radius: ['50%', '74%'],
        center: ['50%', '42%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#ffffff', borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 12, fontWeight: 500 } },
        data: items.map((item) => ({
          name: SEVERITY_LABELS[item.severity] || item.severity,
          value: item.count,
          itemStyle: { color: SEVERITY_COLORS[item.severity] || SEVERITY_COLORS.unknown },
        })),
      },
    ],
  }
})
</script>

<template>
  <div v-loading="loading" class="trend">
    <div class="trend-toolbar">
      <el-radio-group v-model="days" size="small">
        <el-radio-button v-for="item in DAY_OPTIONS" :key="item.value" :value="item.value">
          {{ item.label }}
        </el-radio-button>
      </el-radio-group>
    </div>

    <template v-if="hasData">
      <div class="kpi-row">
        <div class="kpi">
          <span class="kpi-value">{{ trend.total_records }}</span>
          <span class="kpi-label">窗口内记录</span>
        </div>
        <div class="kpi">
          <span class="kpi-value">{{ trend.active_days }}</span>
          <span class="kpi-label">有记录的天数</span>
        </div>
        <div class="kpi">
          <span class="kpi-value">{{ trend.body_site_count }}</span>
          <span class="kpi-label">涉及部位</span>
        </div>
      </div>

      <section class="chart-block">
        <h4>程度随时间的变化</h4>
        <p class="chart-hint">纵轴是自述程度的编码（未标记 0 / 轻微 1 / 中等 2 / 较明显 3）。</p>
        <EChart :option="trendOption" height="240px" />
      </section>

      <div class="chart-grid">
        <section class="chart-block">
          <h4>部位分布</h4>
          <p class="chart-hint">出现次数最多的前 8 个部位。</p>
          <EChart :option="siteOption" height="240px" />
        </section>
        <section class="chart-block">
          <h4>程度构成</h4>
          <p class="chart-hint">窗口内各档位的占比。</p>
          <EChart :option="severityOption" height="240px" />
        </section>
      </div>
    </template>

    <el-empty v-else-if="!loading" description="这个时间窗口内还没有记录">
      <template #description>
        <p>这个时间窗口内还没有记录。</p>
        <p v-if="hasEarlier" class="earlier-hint">
          你在更早的时间有 {{ trend.total_records_all }} 条记录，可以切换更大的时间范围查看。
        </p>
      </template>
    </el-empty>

    <p class="disclaimer">{{ trend?.disclaimer }}</p>
  </div>
</template>

<style scoped>
.trend-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}

.kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--brand-weak);
}

.kpi-value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--text);
}

.kpi-label {
  font-size: 12px;
  color: var(--text-secondary);
}

.chart-block {
  margin-bottom: 16px;
  padding: 12px 14px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card-bg);
  box-shadow: var(--card-shadow);
}

.chart-block h4 {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.chart-hint {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-secondary);
}

.chart-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.earlier-hint {
  margin-top: 6px;
  font-size: 12.5px;
  color: var(--text-secondary);
}

.disclaimer {
  margin-top: 4px;
  padding-top: 10px;
  border-top: 1px solid var(--divider);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

@media (max-width: 720px) {
  .chart-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
