<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createHealthRecord,
  deleteHealthRecord,
  exportHealthRecords,
  listHealthRecords,
  updateHealthRecord,
} from '../../api'
import { useSidebar } from '../../composables/useSidebar'
import HealthTrendChart from './HealthTrendChart.vue'

const visible = defineModel({ type: Boolean, default: false })
const { isMobile } = useSidebar()
const drawerSize = computed(() => (isMobile.value ? '100%' : '680px'))
const PAGE_SIZE = 50
const records = ref([])
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(false)
const dialogOpen = ref(false)
const saving = ref(false)
const editId = ref('')
/** 默认落在趋势页：记录填了就是要看变化的，时间线是次要视角 */
const activeTab = ref('trend')
/** 增删改后自增，驱动趋势图重新拉数（趋势与时间线共用同一份源数据） */
const trendKey = ref(0)
const form = reactive({
  recorded_on: todayLocal(),
  body_site: '',
  symptoms: '',
  severity: 'unknown',
  notes: '',
})

function todayLocal() {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 10)
}

function resetForm(record = null) {
  editId.value = record?.record_id || ''
  form.recorded_on = record?.recorded_on || todayLocal()
  form.body_site = record?.body_site || ''
  form.symptoms = record?.symptoms || ''
  form.severity = record?.severity || 'unknown'
  form.notes = record?.notes || ''
  dialogOpen.value = true
}

async function loadRecords(append = false) {
  if (append) loadingMore.value = true
  else loading.value = true
  try {
    const offset = append ? records.value.length : 0
    const data = await listHealthRecords(PAGE_SIZE, offset)
    records.value = append ? [...records.value, ...data] : data
    hasMore.value = data.length === PAGE_SIZE
  } catch (err) {
    ElMessage.error(err?.message || '加载健康记录失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function submitRecord() {
  if (!form.body_site.trim() || !form.symptoms.trim()) {
    ElMessage.warning('请填写身体部位和症状表现')
    return
  }
  saving.value = true
  try {
    const payload = { ...form }
    if (editId.value) await updateHealthRecord(editId.value, payload)
    else await createHealthRecord(payload)
    ElMessage.success(editId.value ? '记录已更新' : '记录已添加')
    dialogOpen.value = false
    await refreshAfterMutation()
  } catch (err) {
    ElMessage.error(err?.message || '保存记录失败')
  } finally {
    saving.value = false
  }
}

/**
 * 记录变动后同步两个视图。
 *
 * 趋势图每次都要刷新（它依赖全部窗口数据）；时间线只在当前正被查看时刷新，
 * 否则会白白发一次没人看的请求。
 */
async function refreshAfterMutation() {
  trendKey.value += 1
  if (activeTab.value === 'timeline') await loadRecords()
}

async function removeRecord(record) {
  try {
    await ElMessageBox.confirm('删除后无法恢复，确定删除这条个人健康记录吗？', '删除记录', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteHealthRecord(record.record_id)
    ElMessage.success('记录已删除')
    await refreshAfterMutation()
  } catch (err) {
    ElMessage.error(err?.message || '删除记录失败')
  }
}

async function downloadExport() {
  try {
    const blob = await exportHealthRecords()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `skin-health-records-${todayLocal()}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    ElMessage.success('个人记录已导出')
  } catch (err) {
    ElMessage.error(err?.message || '导出失败')
  }
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

watch(
  [visible, activeTab],
  ([open, tab]) => {
    // 时间线按需加载：默认停在趋势页时不必拉列表
    if (open && tab === 'timeline' && !records.value.length) loadRecords()
  },
  { immediate: true }
)
</script>

<template>
  <el-drawer
    v-model="visible"
    title="个人健康记录"
    direction="rtl"
    :size="drawerSize"
    :destroy-on-close="false"
  >
    <div class="records-intro">
      <p>记录由你主动填写，仅本人可见。这里用于回顾症状变化，不构成诊断，也不保存照片。</p>
      <div class="toolbar">
        <el-button @click="downloadExport">导出 JSON</el-button>
        <el-button type="primary" @click="resetForm()">新增记录</el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="records-tabs">
      <el-tab-pane label="病程趋势" name="trend">
        <HealthTrendChart :refresh-key="trendKey" />
      </el-tab-pane>

      <el-tab-pane label="记录时间线" name="timeline" lazy>
        <div v-loading="loading" class="timeline-wrap">
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
                <div class="record-actions">
                  <el-button link type="primary" @click="resetForm(record)">编辑</el-button>
                  <el-button link type="danger" @click="removeRecord(record)">删除</el-button>
                </div>
              </article>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else-if="!loading" description="还没有健康记录" />

          <div v-if="hasMore" class="load-more">
            <el-button :loading="loadingMore" @click="loadRecords(true)">加载更多</el-button>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="dialogOpen" :title="editId ? '编辑记录' : '新增记录'" width="440px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="记录日期" required>
          <el-date-picker
            v-model="form.recorded_on"
            type="date"
            value-format="YYYY-MM-DD"
            format="YYYY-MM-DD"
            :clearable="false"
            class="full"
          />
        </el-form-item>
        <el-form-item label="身体部位" required>
          <el-input v-model="form.body_site" maxlength="80" placeholder="例如：左侧面颊" />
        </el-form-item>
        <el-form-item label="症状表现" required>
          <el-input
            v-model="form.symptoms"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="记录你观察到的表现、出现时间或变化"
          />
        </el-form-item>
        <el-form-item label="自述程度">
          <el-select v-model="form.severity" class="full">
            <el-option label="未标记" value="unknown" />
            <el-option label="轻微" value="mild" />
            <el-option label="中等" value="moderate" />
            <el-option label="较明显" value="severe" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input
            v-model="form.notes"
            type="textarea"
            :rows="2"
            maxlength="2000"
            show-word-limit
            placeholder="可记录诱因、护理或就医情况"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitRecord">保存</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.records-intro {
  margin-bottom: 18px;
  padding: 12px 14px;
  border: 1px solid var(--brand-border);
  border-radius: var(--radius);
  background: var(--brand-weak);
  color: var(--text-secondary);
  font-size: 12.5px;
  line-height: 1.6;
}

.toolbar {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}

.timeline-wrap {
  min-height: 220px;
}

.records-tabs {
  margin-top: 4px;
}

.records-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
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

.record-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
}

.load-more {
  display: flex;
  justify-content: center;
  padding: 8px 0 20px;
}

.full {
  width: 100%;
}
</style>
