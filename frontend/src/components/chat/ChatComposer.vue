<script setup>
import { computed } from 'vue'
import ImageUploader from './ImageUploader.vue'

/**
 * 底部输入区：图片上传 + 文本输入 + 发送 / 停止 + 知识库开关。
 * 三个 v-model 分别绑定文本、图片与知识库开关。
 */
const input = defineModel('input', { type: String, default: '' })
const image = defineModel('image', { type: Object, default: null })
const useKnowledge = defineModel('useKnowledge', { type: Boolean, default: true })

const props = defineProps({
  generating: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits(['send', 'stop'])

const canSend = computed(() => !props.generating && (!!input.value.trim() || !!image.value))

function onKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    emit('send')
  }
}
</script>

<template>
  <footer class="composer">
    <div class="composer-inner">
      <ImageUploader v-model="image" :disabled="generating" />

      <div class="input-row">
        <el-input
          v-model="input"
          class="textarea"
          type="textarea"
          resize="none"
          :autosize="{ minRows: 1, maxRows: 6 }"
          placeholder="描述症状，或上传皮肤图像后发送…"
          @keydown="onKeydown"
        />
        <div class="actions">
          <el-button v-if="generating" type="danger" plain @click="emit('stop')">停止生成</el-button>
          <el-button v-else type="primary" :disabled="!canSend" @click="emit('send')">发送</el-button>
        </div>
      </div>

      <div class="composer-foot">
        <div class="foot-left">
          <el-switch v-model="useKnowledge" size="small" />
          <span class="foot-label">启用知识库检索</span>
        </div>
        <span class="foot-hint">Enter 发送 · Shift + Enter 换行</span>
      </div>
    </div>
  </footer>
</template>

<style scoped>
.composer {
  flex: 0 0 auto;
  padding: 12px 24px 18px;
  background: var(--bg);
}

.composer-inner {
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-shadow);
}

.input-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.textarea {
  flex: 1;
}

:deep(.textarea .el-textarea__inner) {
  border: none;
  box-shadow: none;
  padding: 4px 2px;
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.7;
  color: var(--text);
  background: transparent;
  resize: none;
}

:deep(.textarea .el-textarea__inner:focus) {
  box-shadow: none;
}

.actions {
  flex: 0 0 auto;
  padding-bottom: 2px;
}

.actions .el-button {
  min-width: 84px;
}

.composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--divider);
}

.foot-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.foot-label {
  font-size: 12.5px;
  color: var(--text-secondary);
}

.foot-hint {
  font-size: 11.5px;
  color: var(--text-muted);
}

@media (max-width: 640px) {
  .composer {
    padding: 8px 10px 12px;
  }

  .composer-inner {
    padding: 10px 12px;
  }

  .foot-hint {
    display: none;
  }
}
</style>
