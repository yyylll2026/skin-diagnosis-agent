<script setup>
import { computed } from 'vue'
import { scorePercent } from '../../utils/common'

/**
 * 参考来源 / 检索命中条目。
 * SourceList（对话内）与 KnowledgePanel（检索测试）共用，
 * 两处原先各有一份几乎相同的结构与样式，已合并到这里。
 */
const props = defineProps({
  item: {
    type: Object,
    required: true,
  },
})

const scoreText = computed(() => scorePercent(props.item?.score))
</script>

<template>
  <div class="source-card">
    <div class="source-head">
      <span class="source-title" :title="item.title">{{ item.title }}</span>
      <span class="source-score" :title="`相关度 ${scoreText}`">{{ scoreText }}</span>
    </div>

    <div v-if="item.category || item.source" class="source-tags">
      <el-tag v-if="item.category" size="small" type="info" effect="plain">
        {{ item.category }}
      </el-tag>
      <span v-if="item.source" class="source-file" :title="item.source">{{ item.source }}</span>
    </div>

    <p v-if="item.snippet" class="source-snippet">{{ item.snippet }}</p>
  </div>
</template>

<style scoped>
.source-card {
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--brand-border);
  border-radius: var(--radius-sm);
  background: var(--surface-soft);
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
  font-variant-numeric: tabular-nums;
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
</style>
