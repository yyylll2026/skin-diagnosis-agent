<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteDocument, listDocuments, searchKnowledge, uploadDocument } from '../api'

const visible = defineModel({ type: Boolean, default: false })

const CATEGORIES = ['皮肤疾病', '用药指南', '护理建议', '其他']

const uploadRef = ref(null)
const pendingFile = ref(null)
const category = ref(CATEGORIES[0])
const uploading = ref(false)

const documents = ref([])
const loadingDocs = ref(false)
const removingId = ref('')

const query = ref('')
const searching = ref(false)
const searched = ref(false)
const results = ref([])

const hasPending = computed(() => !!pendingFile.value)

function normalizeDoc(raw) {
  const item = raw || {}
  return {
    doc_id: item.doc_id ?? item.id ?? item.document_id ?? '',
    title: item.title ?? item.filename ?? item.name ?? item.file_name ?? '未命名文档',
    category: item.category ?? item.type ?? '其他',
    chunk_count: item.chunk_count ?? item.chunks ?? item.num_chunks ?? item.chunk_num ?? 0,
    created_at: item.created_at ?? item.created_time ?? item.upload_time ?? item.time ?? '',
  }
}

function pickList(data) {
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.documents)) return data.documents
  if (Array.isArray(data?.items)) return data.items
  if (Array.isArray(data?.data)) return data.data
  return []
}

async function loadDocuments() {
  loadingDocs.value = true
  try {
    const data = await listDocuments()
    documents.value = pickList(data).map(normalizeDoc)
  } catch (err) {
    documents.value = []
    ElMessage.error(err.message || '获取文档列表失败')
  } finally {
    loadingDocs.value = false
  }
}

function onFileChange(uploadFile) {
  pendingFile.value = uploadFile?.raw || null
}

function onFileRemove() {
  pendingFile.value = null
}

async function submitUpload() {
  if (!pendingFile.value) {
    ElMessage.warning('请先选择要上传的文档')
    return
  }
  uploading.value = true
  try {
    await uploadDocument(pendingFile.value, category.value)
    ElMessage.success('上传成功，文档已入库')
    pendingFile.value = null
    uploadRef.value?.clearFiles?.()
    await loadDocuments()
  } catch (err) {
    ElMessage.error(err.message || '上传失败')
  } finally {
    uploading.value = false
  }
}

async function removeDoc(row) {
  if (!row.doc_id) {
    ElMessage.error('该文档缺少标识，无法删除')
    return
  }
  try {
    await ElMessageBox.confirm(`确定删除「${row.title}」吗？该操作不可撤销。`, '删除文档', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch (err) {
    return
  }
  removingId.value = row.doc_id
  try {
    await deleteDocument(row.doc_id)
    ElMessage.success('已删除')
    await loadDocuments()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  } finally {
    removingId.value = ''
  }
}

function normalizeHit(raw, index) {
  const item = raw || {}
  const score = typeof item.score === 'number' ? item.score : null
  return {
    key: item.doc_id ?? item.id ?? `hit-${index}`,
    title: item.title ?? item.source ?? item.filename ?? `命中片段 ${index + 1}`,
    source: item.source ?? '',
    category: item.category ?? '',
    text: item.snippet ?? item.content ?? item.text ?? item.chunk ?? '',
    scoreText: score === null ? '—' : `${Math.round(score <= 1 ? score * 100 : score)}%`,
  }
}

async function runSearch() {
  const text = query.value.trim()
  if (!text) {
    ElMessage.warning('请输入检索内容')
    return
  }
  searching.value = true
  try {
    const data = await searchKnowledge(text, 5)
    const list = Array.isArray(data) ? data : data?.results || data?.items || data?.data || []
    results.value = (Array.isArray(list) ? list : []).map(normalizeHit)
    searched.value = true
  } catch (err) {
    results.value = []
    searched.value = true
    ElMessage.error(err.message || '检索失败')
  } finally {
    searching.value = false
  }
}

watch(visible, (open) => {
  if (open) {
    loadDocuments()
  }
})
</script>

<template>
  <el-drawer
    v-model="visible"
    title="知识库管理"
    direction="rtl"
    size="620px"
    :destroy-on-close="false"
  >
    <section class="section">
      <h3 class="section-title">上传文档</h3>
      <el-upload
        ref="uploadRef"
        drag
        :auto-upload="false"
        :limit="1"
        :show-file-list="true"
        :on-change="onFileChange"
        :on-remove="onFileRemove"
        accept=".txt,.md,.pdf,.docx,.doc,.csv"
      >
        <div class="upload-hint">
          <p class="upload-main">将文件拖到此处，或点击选择</p>
          <p class="upload-sub">支持 txt / md / pdf / docx 等文本类文档</p>
        </div>
      </el-upload>

      <div class="upload-bar">
        <el-select v-model="category" class="category-select" placeholder="选择分类">
          <el-option v-for="item in CATEGORIES" :key="item" :label="item" :value="item" />
        </el-select>
        <el-button type="primary" :loading="uploading" :disabled="!hasPending" @click="submitUpload">
          上传并入库
        </el-button>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <h3 class="section-title">文档列表</h3>
        <el-button link type="primary" :loading="loadingDocs" @click="loadDocuments">刷新</el-button>
      </div>

      <el-table v-loading="loadingDocs" :data="documents" size="small" empty-text="暂无文档">
        <el-table-column prop="title" label="文件名" min-width="150" show-overflow-tooltip />
        <el-table-column label="分类" width="96">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" type="info">{{ row.category }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="分块数" width="78" align="center" />
        <el-table-column prop="created_at" label="入库时间" width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="72" align="center">
          <template #default="{ row }">
            <el-button
              link
              type="danger"
              :loading="removingId === row.doc_id"
              @click="removeDoc(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="section">
      <h3 class="section-title">检索测试</h3>
      <div class="search-bar">
        <el-input
          v-model="query"
          placeholder="输入问题，例如：湿疹的日常护理建议"
          clearable
          @keydown.enter="runSearch"
        />
        <el-button type="primary" :loading="searching" @click="runSearch">检索</el-button>
      </div>

      <div v-if="results.length" class="hits">
        <div v-for="(hit, index) in results" :key="`${hit.key}-${index}`" class="hit">
          <div class="hit-head">
            <span class="hit-title">{{ hit.title }}</span>
            <span class="hit-score">{{ hit.scoreText }}</span>
          </div>
          <div class="hit-tags">
            <el-tag v-if="hit.category" size="small" effect="plain" type="info">
              {{ hit.category }}
            </el-tag>
            <span v-if="hit.source" class="hit-source">{{ hit.source }}</span>
          </div>
          <p v-if="hit.text" class="hit-text">{{ hit.text }}</p>
        </div>
      </div>
      <p v-else-if="searched && !searching" class="hits-empty">未检索到相关内容</p>
    </section>
  </el-drawer>
</template>

<style scoped>
.section {
  padding-bottom: 18px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--divider);
}

.section:last-child {
  border-bottom: none;
  margin-bottom: 0;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 12px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.section-head .section-title {
  margin-bottom: 0;
}

.upload-hint {
  padding: 6px 0;
}

.upload-main {
  font-size: 13.5px;
  color: var(--text);
}

.upload-sub {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--text-muted);
}

.upload-bar {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}

.category-select {
  width: 160px;
}

.search-bar {
  display: flex;
  gap: 10px;
}

.hits {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hit {
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--brand-border);
  border-radius: var(--radius-sm);
  background: #fbfdfe;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.hit-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hit-score {
  flex: 0 0 auto;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--brand);
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
  border-radius: 999px;
  padding: 1px 8px;
}

.hit-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
}

.hit-source {
  font-size: 11.5px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hit-text {
  margin-top: 6px;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text-secondary);
}

.hits-empty {
  margin-top: 12px;
  font-size: 12.5px;
  color: var(--text-muted);
  text-align: center;
}
</style>
