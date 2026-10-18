<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { listPatientRecords, listPatients } from '../../api'
import { useSidebar } from '../../composables/useSidebar'
import HealthTrendChart from './HealthTrendChart.vue'

/**
 * 患者（医生）工作台抽屉。
 *
 * 数据来源是服务端 `health_records:read_all` 权限点守卫的四个只读端点，
 * 记录的编辑/删除接口只认「记录归属者本人」，所以这里**刻意不提供任何写操作**，
 * 也不放导出按钮——改记录请患者自己来，医生端只读查看。
 *
 * 两层视图：患者索引（列表）→ 单个患者详情（趋势图 + 时间线）。
 * 趋势图复用 HealthTrendChart，通过 patientId 切换数据源。
 */
const visible = defineModel({ type: Boolean, default: false })
const { isMobile } = useSidebar()

const PAGE_SIZE = 200
const patients = ref([])
const loading = ref(false)

/** 当前查看的患者；null 表示停在索引页 */
const activePatient = ref(null)
const records = ref([])
const recordsLoading = ref(false)
const activeTab = ref('trend')
/** 记录数没有写操作，trendKey 保留为 0，让 HealthTrendChart 自己按窗口变化重拉 */
const trendKey = ref(0)

const patientTitle = computed(() => {
  const p = activePatient.value
  if (!p) return ''
  return p.display_name && p.display_name !== p.username
    ? `${p.display_name}（${p.username}）`
    : p.username || p.display_name
})

function patientName(row) {
  return row.display_name && row.display_name !== row.username
    ? `${row.display_name}（${row.username}）`
    : row.display_name || row.username
}

function formatDate(value) {
  if (!value) return '日期未知'
  const [year, month, day] = value.split('-')
  return `${year}年${Number(month)}月${Number(day)}日`
}

function severityLabel(value) {
  return ({ unknown: '未标记', mild: '轻微', moderate: '中等', severe: '较明显' })[value] || '未标记'
}

function severityType(value) {
  return ({ mild: 'success', moderate: 'warning', severe: 'danger' })[value] || 'info'
}

async function loadPatients() {
  loading.value = true
  try {
    patients.value = await listPatients(PAGE_SIZE)
  } catch (err) {
    ElMessage.error(err?.message || '加载患者列表失败')
  } finally {
    loading.value = false
  }
}

async function openPatient(row) {
  activePatient.value = row
  records.value = []
  activeTab.value = 'trend'
}

function backToIndex() {
  activePatient.value = null
}

async function loadRecords() {
  if (!activePatient.value) return
  recordsLoading.value = true
  try {
    records.value = await listPatientRecords(activePatient.value.user_id, PAGE_SIZE)
  } catch (err) {
    ElMessage.error(err?.message || '加载患者记录失败')
  } finally {
    recordsLoading.value = false
  }
}

watch(
  visible,
  (open) => {
    if (!open) return
    // 关到索引页再重拉，避免上一位患者的数据残留在列表里
    activePatient.value = null
    loadPatients()
  },
  { immediate: true }
)

watch(activeTab, (tab) => {
  if (tab === 'timeline' && !records.value.length) loadRecords()
})
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="activePatient ? `患者记录 · ${patientTitle}` : '患者工作台'"
    direction="rtl"
    :size="isMobile ? '100%' : '760px'"
    :destroy-on-close="false"
  >
    <!-- 索引：患者列表 -->
    <template v-if="!activePatient">
      <div class="workbench-intro">
        <p>列出所有有健康记录的启用账号，按最近记录时间倒序。仅只读查看，不保存照片，不构成诊断。</p>
      </div>
      <el-table
        v-loading="loading"
        :data="patients"
        size="small"
        class="table"
        empty-text="还没有患者填写健康记录"
      >
        <el-table-column label="患者" min-width="150">
          <template #default="{ row }">{{ patientName(row) }}</template>
        </el-table-column>
        <el-table-column prop="record_count" label="记录数" width="80" />
        <el-table-column prop="latest_recorded_on" label="最近记录" width="120" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openPatient(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <!-- 详情：单个患者的趋势与时间线 -->
    <template v-else>
      <div class="detail-head">
        <el-button size="small" @click="backToIndex">← 返回列表</el-button>
        <el-radio-group v-model="activeTab" size="small">
          <el-radio-button value="trend">病程趋势</el-radio-button>
          <el-radio-button value="timeline">记录时间线</el-radio-button>
        </el-radio-group>
      </div>

      <div v-if="activeTab === 'trend'" class="trend-wrap">
        <HealthTrendChart :refresh-key="trendKey" :patient-id="activePatient.user_id" />
      </div>

      <div v-else v-loading="recordsLoading" class="timeline-wrap">
        <el-timeline v-if="records.length">
          <el-timeline-item
            v-for="record in records"
            :key="record.record_id"
            :timestamp="formatDate(record.recorded_on)"
            placement="top"
            color="#0e7490"
          >
            <article class="record-card">
              <div class="record-head">
                <h3>{{ record.body_site }}</h3>
                <el-tag :type="severityType(record.severity)" effect="plain" size="small">
                  {{ severityLabel(record.severity) }}
                </el-tag>
              </div>
              <p class="symptoms">{{ record.symptoms }}</p>
              <p v-if="record.notes" class="notes">{{ record.notes }}</p>
            </article>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else-if="!recordsLoading" description="该患者还没有健康记录" />
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.workbench-intro {
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid var(--brand-border);
  border-radius: var(--radius);
  background: var(--brand-weak);
  color: var(--text-secondary);
  font-size: 12.5px;
  line-height: 1.6;
}

.table {
  width: 100%;
}

.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.trend-wrap {
  min-height: 220px;
}

.timeline-wrap {
  min-height: 220px;
}

.record-card {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card-bg);
  box-shadow: var(--card-shadow);
}

.record-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.record-head h3 {
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.symptoms {
  margin-top: 8px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text);
  line-height: 1.65;
}

.notes {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--divider);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-secondary);
  font-size: 12.5px;
}
</style>
