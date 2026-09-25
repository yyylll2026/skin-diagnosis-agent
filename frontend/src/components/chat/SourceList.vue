<script setup>
import { computed, ref } from 'vue'
import SourceCard from '../common/SourceCard.vue'
import { normalizeSource } from '../../utils/common'

const props = defineProps({
  sources: {
    type: Array,
    default: () => [],
  },
})

const activeNames = ref([])

const items = computed(() => {
  const list = Array.isArray(props.sources) ? props.sources : []
  return list.map((item, index) => normalizeSource(item, index))
})
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
          <li v-for="(item, index) in items" :key="`${item.key}-${index}`">
            <SourceCard :item="item" />
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

.source-empty {
  font-size: 12.5px;
  color: var(--text-muted);
  padding: 4px 2px;
}
</style>
