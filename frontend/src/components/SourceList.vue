<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  sources: {
    type: Array,
    default: () => [],
  },
})

const activeNames = ref([])

const items = computed(() => {
  const list = Array.isArray(props.sources) ? props.sources : []
  return list.map((item, index) => ({
    doc_id: item?.doc_id ?? item?.id ?? `source-${index}`,
    title: item?.title || item?.source || `参考片段 ${index + 1}`,
    source: item?.source || '',
    category: item?.category || '',
    snippet: item?.snippet || item?.content || item?.text || '',
    score: typeof item?.score === 'number' ? item.score : null,
  }))
})

function scorePercent(score) {
  if (typeof score !== 'number' || Number.isNaN(score)) return '—'
  const value = score <= 1 ? score * 100 : score
  return `${Math.round(value)}%`
}
</script>

<template>
  <div class="source-list">
    <el-collapse v-model="activeNames" class="collapse">
      <el-collapse-item :name="'sources'" class="collapse-item">
        <template #title>
          <span class="collapse-title">
            <span class="collapse-dot"></span>
            参考来源 ({{ items.length }})
          </span>
        </template>

        <ul class="source-items">
          <li v-for="(item, index) in items" :key="`${item.doc_id}-${index}`" class="source-item">
            <div class="source-head">
              <span class="source-title">{{ item.title }}</span>
              <span class="source-score" :title="`相关度 ${scorePercent(item.score)}`">
                {{ scorePercent(item.score) }}
              </span>
            </div>

            <div class="source-tags">
              <el-tag v-if="item.category" size="small" type="info" effect="plain">
                {{ item.category }}
              </el-tag>
              <span v-if="item.source" class="source-file" :title="item.source">{{ item.source }}</span>
            </div>

            <p v-if="item.snippet" class="source-snippet">{{ item.snippet }}</p>
          </li>
          <li v-if="!items.length" class="source-empty">暂无参考来源</li>
        </ul>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.source-list {
  margin-top: 12px;
  border-top: 1px dashed var(--border);
  padding-top: 8px;
}

.collapse,
.collapse-item {
  border: none;
}

:deep(.el-collapse-item__header) {
  height: 34px;
  line-height: 34px;
  border-bottom: none;
  font-size: 13px;
  color: var(--text-secondary);
  background: transparent;
}

:deep(.el-collapse-item__wrap) {
  border-bottom: none;
  background: transparent;
}

:deep(.el-collapse-item__content) {
  padding-bottom: 4px;
}

.collapse-title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-weight: 500;
  color: var(--text-secondary);
}

.collapse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brand-aux);
}

.source-items {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.source-item {
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--brand-border);
  border-radius: var(--radius-sm);
  background: #fbfdfe;
}

.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.source-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-score {
  flex: 0 0 auto;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--brand);
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
  border-radius: 999px;
  padding: 1px 8px;
}

.source-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
}

.source-file {
  font-size: 11.5px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-snippet {
  margin-top: 6px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.source-empty {
  font-size: 12.5px;
  color: var(--text-muted);
  padding: 4px 2px;
}
</style>
