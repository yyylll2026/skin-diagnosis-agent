<script setup>
/**
 * 检索效果评测面板。
 *
 * 解决的问题很具体：**改了分块、权重、阈值之后，检索到底变好了还是变坏了？**
 * 以前只能随手搜一条看看感觉，现在可以在固定标注集上跑出一组指标来对比。
 *
 * 三个设计取舍：
 * 1. **标注集放在前端（文本框）而不是服务端**。评测是「一次实验」，
 *    不该在服务端留下状态；想留档就把文本框内容存到自己的仓库里。
 * 2. **指标 + 逐条明细一起看**。只有总分的话，指标掉了无从下手；
 *    明细能直接告诉你是哪几条样本开始检索不到了。
 * 3. **保留上一次运行结果做对比**。调参的价值就在「变化量」，
 *    只看绝对值没有意义，所以每次运行都会显示相对上一次的增减。
 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getEvalStarter, runRetrievalEval } from '../../api'

const props = defineProps({
  /** 已入库文档，用于提示可标注的标题 */
  documents: { type: Array, default: () => [] },
})

const CATEGORIES = ['皮肤疾病', '用药指南', '护理建议', '其他']

/** 模板里给的示例标注长这样，方便用户照着改 */
const PLACEHOLDER = `{"query":"面部红斑伴鳞屑如何鉴别","relevant":["银屑病诊疗指南"],"category":"皮肤疾病"}
{"query":"特应性皮炎的一线外用药有哪些","relevant":["特应性皮炎外用治疗"],"category":"用药指南"}`

const datasetText = ref('')
const topK = ref(5)
const category = ref('')
const rerankMode = ref('auto')
const running = ref(false)
const loadingTemplate = ref(false)
const result = ref(null)
/** 上一次的汇总指标，用来算变化量 */
const previousMetrics = ref(null)
const onlyMisses = ref(false)

const cases = computed(() => result.value?.cases || [])
const visibleCases = computed(() =>
  onlyMisses.value ? cases.value.filter((item) => !item.hit) : cases.value
)

const RERANK_OPTIONS = [
  { label: '跟随服务端配置', value: 'auto' },
  { label: '强制开启重排', value: 'on' },
  { label: '强制关闭重排', value: 'off' },
]

function stripNulls(value) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== null && item !== ''))
}

/**
 * 解析标注集文本，支持两种写法：
 * - 整体一个 JSON（数组，或带 cases 字段的对象）；
 * - JSONL（一行一条），便于用 git 逐行 diff。
 */
function parseCases(text) {
  const trimmed = text.trim()
  if (!trimmed) return []

  try {
    const parsed = JSON.parse(trimmed)
    if (Array.isArray(parsed)) return parsed
    if (parsed && Array.isArray(parsed.cases)) return parsed.cases
  } catch (err) {
    // 不是整体 JSON，按 JSONL 逐行解析
  }

  return trimmed
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('//'))
    .map((line, index) => {
      try {
        return JSON.parse(line)
      } catch (err) {
        throw new Error(`第 ${index + 1} 行不是合法的 JSON`)
      }
    })
}

async function loadTemplate() {
  loadingTemplate.value = true
  try {
    const data = await getEvalStarter()
    datasetText.value = (data?.cases || []).map((item) => JSON.stringify(stripNulls(item))).join('\n')
    ElMessage.success('已载入模板，请把 relevant 换成库里真实的文档标题')
  } catch (err) {
    ElMessage.error(err?.message || '载入模板失败')
  } finally {
    loadingTemplate.value = false
  }
}

function fillTitles() {
  const titles = props.documents.map((item) => item.title).filter(Boolean)
  if (!titles.length) {
    ElMessage.warning('知识库里还没有文档，先在「文档」页签上传')
    return
  }
  navigator.clipboard
    ?.writeText(titles.join('\n'))
    .then(() => ElMessage.success(`已复制 ${titles.length} 个文档标题到剪贴板`))
    .catch(() => ElMessage.info(titles.join('、')))
}

async function run() {
  let parsed
  try {
    parsed = parseCases(datasetText.value)
  } catch (err) {
    ElMessage.error(err.message)
    return
  }
  if (!parsed.length) {
    ElMessage.warning('请先填写标注集（可点「载入模板」）')
    return
  }

  running.value = true
  try {
    const data = await runRetrievalEval({
      cases: parsed,
      top_k: topK.value,
      category: category.value || null,
      use_rerank: rerankMode.value === 'auto' ? null : rerankMode.value === 'on',
    })
    previousMetrics.value = result.value?.metrics || null
    result.value = data
    if (data?.notes) ElMessage.warning(data.notes)
  } catch (err) {
    ElMessage.error(err?.message || '评测失败')
  } finally {
    running.value = false
  }
}

function percent(value) {
  return `${((value ?? 0) * 100).toFixed(1)}%`
}

/** 相对上一次的变化；没有上一次就返回 null，界面不显示 */
function delta(key) {
  if (!previousMetrics.value || !result.value) return null
  return (result.value.metrics[key] ?? 0) - (previousMetrics.value[key] ?? 0)
}

function deltaText(key) {
  const value = delta(key)
  if (value === null) return ''
  if (Math.abs(value) < 0.0005) return '持平'
  const sign = value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(1)}pt`
}

function deltaClass(key) {
  const value = delta(key)
  if (value === null || Math.abs(value) < 0.0005) return 'delta-flat'
  return value > 0 ? 'delta-up' : 'delta-down'
}
</script>

<template>
  <div class="eval">
    <section class="block">
      <div class="block-head">
        <h3>标注集</h3>
        <div class="block-actions">
          <el-button link type="primary" :loading="loadingTemplate" @click="loadTemplate">
            载入模板
          </el-button>
          <el-button link type="primary" :disabled="!documents.length" @click="fillTitles">
            复制文档标题
          </el-button>
        </div>
      </div>
      <p class="hint">
        每行一条 JSON（JSONL），<code>relevant</code> 写期望命中的文档标题或文件名。
        标注绑文档而非片段，换 embedding 重建索引后依然可用。
      </p>
      <el-input
        v-model="datasetText"
        type="textarea"
        :rows="7"
        :placeholder="PLACEHOLDER"
        class="dataset"
      />
    </section>

    <section class="block">
      <h3>运行配置</h3>
      <div class="config-row">
        <label class="field">
          <span>取前 K 条</span>
          <el-input-number v-model="topK" :min="1" :max="20" size="small" controls-position="right" />
        </label>
        <label class="field">
          <span>检索范围</span>
          <el-select v-model="category" size="small" class="field-select">
            <el-option label="全库" value="" />
            <el-option v-for="item in CATEGORIES" :key="item" :label="item" :value="item" />
          </el-select>
        </label>
        <label class="field">
          <span>重排</span>
          <el-select v-model="rerankMode" size="small" class="field-select">
            <el-option
              v-for="item in RERANK_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </label>
      </div>
      <el-button type="primary" :loading="running" class="run-btn" @click="run">
        运行评测
      </el-button>
    </section>

    <template v-if="result">
      <section class="block">
        <div class="block-head">
          <h3>汇总指标</h3>
          <span class="meta">
            {{ result.metrics.case_count }} 条样本 · K={{ result.metrics.k }} ·
            {{ result.elapsed_ms }}ms
            <template v-if="result.use_rerank"> · 已开启重排</template>
          </span>
        </div>

        <div class="kpi-row">
          <div class="kpi">
            <span class="kpi-value">{{ percent(result.metrics.hit_rate) }}</span>
            <span class="kpi-label">命中率 Hit@K</span>
            <span v-if="delta('hit_rate') !== null" class="kpi-delta" :class="deltaClass('hit_rate')">
              {{ deltaText('hit_rate') }}
            </span>
          </div>
          <div class="kpi">
            <span class="kpi-value">{{ (result.metrics.mrr ?? 0).toFixed(3) }}</span>
            <span class="kpi-label">MRR</span>
            <span v-if="delta('mrr') !== null" class="kpi-delta" :class="deltaClass('mrr')">
              {{ deltaText('mrr') }}
            </span>
          </div>
          <div class="kpi">
            <span class="kpi-value">{{ percent(result.metrics.recall_at_k) }}</span>
            <span class="kpi-label">召回率 Recall@K</span>
            <span
              v-if="delta('recall_at_k') !== null"
              class="kpi-delta"
              :class="deltaClass('recall_at_k')"
            >
              {{ deltaText('recall_at_k') }}
            </span>
          </div>
          <div class="kpi">
            <span class="kpi-value">{{ percent(result.metrics.precision_at_k) }}</span>
            <span class="kpi-label">准确率 Precision@K</span>
            <span
              v-if="delta('precision_at_k') !== null"
              class="kpi-delta"
              :class="deltaClass('precision_at_k')"
            >
              {{ deltaText('precision_at_k') }}
            </span>
          </div>
        </div>

        <p v-if="previousMetrics" class="hint delta-hint">
          变化量是相对上一次运行的（每次运行都会覆盖基准）。
        </p>
      </section>

      <section v-if="result.by_category.length > 1" class="block">
        <h3>分组指标</h3>
        <el-table :data="result.by_category" size="small">
          <el-table-column prop="category" label="归类" min-width="90" />
          <el-table-column label="样本" width="58" align="center">
            <template #default="{ row }">{{ row.case_count }}</template>
          </el-table-column>
          <el-table-column label="命中率" width="74" align="center">
            <template #default="{ row }">{{ percent(row.hit_rate) }}</template>
          </el-table-column>
          <el-table-column label="MRR" width="66" align="center">
            <template #default="{ row }">{{ row.mrr.toFixed(3) }}</template>
          </el-table-column>
          <el-table-column label="召回率" width="74" align="center">
            <template #default="{ row }">{{ percent(row.recall_at_k) }}</template>
          </el-table-column>
        </el-table>
      </section>

      <section class="block">
        <div class="block-head">
          <h3>逐条明细</h3>
          <el-checkbox v-model="onlyMisses" size="small">只看未命中</el-checkbox>
        </div>

        <el-table :data="visibleCases" size="small" empty-text="没有符合条件的样本">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="detail">
                <p><span class="detail-key">期望命中</span>{{ row.relevant.join('、') }}</p>
                <p v-if="row.matched.length">
                  <span class="detail-key">实际命中</span>{{ row.matched.join('、') }}
                </p>
                <p v-if="row.missed.length">
                  <span class="detail-key">未召回</span>{{ row.missed.join('、') }}
                </p>
                <p>
                  <span class="detail-key">实际召回</span>
                  {{ row.retrieved.length ? row.retrieved.join(' → ') : '（空）' }}
                </p>
                <p v-if="row.note"><span class="detail-key">备注</span>{{ row.note }}</p>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="query" label="问题" min-width="150" show-overflow-tooltip />
          <el-table-column label="结果" width="72" align="center">
            <template #default="{ row }">
              <el-tag :type="row.hit ? 'success' : 'danger'" size="small" effect="plain">
                {{ row.hit ? '命中' : '未命中' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="首个命中" width="76" align="center">
            <template #default="{ row }">{{ row.first_hit_rank ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="召回" width="66" align="center">
            <template #default="{ row }">{{ percent(row.recall) }}</template>
          </el-table-column>
          <el-table-column label="耗时" width="66" align="center">
            <template #default="{ row }">{{ row.elapsed_ms }}ms</template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <el-empty v-else description="还没有运行过评测" />
  </div>
</template>

<style scoped>
.block {
  padding-bottom: 16px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--divider);
}

.block:last-child {
  border-bottom: none;
  margin-bottom: 0;
}

.block h3 {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 10px;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.block-head h3 {
  margin-bottom: 0;
}

.block-actions {
  display: flex;
  gap: 4px;
}

.hint {
  margin-bottom: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--brand-weak);
  font-size: 11.5px;
}

.dataset :deep(textarea) {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  line-height: 1.6;
}

.config-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
}

.field {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-secondary);
}

.field-select {
  width: 132px;
}

.run-btn {
  width: 100%;
}

.meta {
  font-size: 12px;
  color: var(--text-secondary);
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--brand-weak);
}

.kpi-value {
  font-size: 17px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--text);
}

.kpi-label {
  font-size: 11.5px;
  color: var(--text-secondary);
}

.kpi-delta {
  font-size: 11px;
}

.delta-up {
  color: #0f766e;
}

.delta-down {
  color: #b91c1c;
}

.delta-flat {
  color: var(--text-secondary);
}

.delta-hint {
  margin-top: 8px;
  margin-bottom: 0;
}

.detail {
  padding: 4px 10px 8px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text);
}

.detail-key {
  display: inline-block;
  min-width: 62px;
  margin-right: 8px;
  color: var(--text-secondary);
}

@media (max-width: 720px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
