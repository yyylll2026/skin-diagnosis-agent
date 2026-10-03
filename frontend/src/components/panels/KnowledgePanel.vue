<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteDocument, listDocuments, searchKnowledge, uploadDocument } from '../../api'
import SourceCard from '../common/SourceCard.vue'
import RetrievalEvalPanel from './RetrievalEvalPanel.vue'
import { useSidebar } from '../../composables/useSidebar'
import { normalizeSource, pickList } from '../../utils/common'

const visible = defineModel({ type: Boolean, default: false })

const { isMobile } = useSidebar()

/** 窄屏下抽屉占满全宽 */
const drawerSize = computed(() => (isMobile.value ? '100%' : '620px'))

/** 默认落在文档页：上传/查看文档是最高频的动作 */
const activeTab = ref('docs')

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

async function runSearch() {
  const text = query.value.trim()
  if (!text) {
    ElMessage.warning('请输入检索内容')
    return
  }
  searching.value = true
  try {
    const data = await searchKnowledge(text, 5)
    const list = pickList(data, ['results', 'items', 'data'])
    results.value = list.map((item, index) => normalizeSource(item, index, '命中片段'))
    searched.value = true
  } catch (err) {
    results.value = []
    searched.value = true
    ElMessage.error(err.message || '检索失败')
  } finally {
    searching.value = false
  }
}

// immediate：面板由父级懒加载（首次打开才挂载），
// 挂载时 visible 已为 true，需要立即加载一次文档列表
watch(
  visible,
  (open) => {
    if (open) loadDocuments()
  },
  { immediate: true }
)
</script>

<template>
  <el-drawer
    v-model="visible"
    title="知识库管理"
    direction="rtl"
    :size="drawerSize"
    :destroy-on-close="false"
  >
    <el-tabs v-model="activeTab" class="kb-tabs">
      <el-tab-pane label="文档" name="docs">
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
            <el-button
              type="primary"
              :loading="uploading"
              :disabled="!hasPending"
              @click="submitUpload"
            >
              上传并入库
            </el-button>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <h3 class="section-title">文档列表</h3>
            <el-button link type="primary" :loading="loadingDocs" @click="loadDocuments">
              刷新
            </el-button>
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
      </el-tab-pane>

      <el-tab-pane label="检索测试" name="search" lazy>
        <section class="section">
          <h3 class="section-title">单条检索</h3>
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
            <SourceCard v-for="(hit, index) in results" :key="`${hit.key}-${index}`" :item="hit" />
          </div>
          <p v-else-if="searched && !searching" class="hits-empty">未检索到相关内容</p>
        </section>
      </el-tab-pane>

      <el-tab-pane label="效果评测" name="eval" lazy>
        <RetrievalEvalPanel :documents="documents" />
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>

<style scoped>
.kb-tabs :deep(.el-tabs__header) {
  margin-bottom: 16px;
}

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

.hits-empty {
  margin-top: 12px;
  font-size: 12.5px;
  color: var(--text-muted);
  text-align: center;
}
</style>
