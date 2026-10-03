<script setup>
import { computed } from 'vue'
import DiagnosisCard from './DiagnosisCard.vue'
import ClarifyCard from './ClarifyCard.vue'
import SourceList from './SourceList.vue'
import { renderMarkdown } from '../../utils/markdown'

/**
 * 助手消息卡片：节点状态、Markdown 正文、诊断 / 澄清 / 来源卡片、错误态。
 *
 * 独立成组件的关键收益：流式输出时每个 token 都会改动 content，
 * markdown 渲染（v-html）被收敛在本组件的 computed 里，
 * 只会重算当前这一条消息，而不是像拆分前那样整列表重跑。
 */
const emit = defineEmits(['retry'])

const props = defineProps({
  message: {
    type: Object,
    required: true,
  },
})

const html = computed(() => renderMarkdown(props.message.content))

const showNodeStatus = computed(
  () => props.message.status === 'loading' && !!props.message.nodeText
)
const showThinking = computed(
  () =>
    props.message.status === 'loading' && !props.message.content && !props.message.nodeText
)
const hasSources = computed(
  () => Array.isArray(props.message.sources) && props.message.sources.length > 0
)
</script>

<template>
  <div class="row row-assistant">
    <div class="assistant-card" :class="{ 'is-error': message.status === 'error' }">
      <div v-if="showNodeStatus" class="node-status">
        <span class="node-spinner"></span>
        <span>{{ message.nodeText }}</span>
        <span v-if="typeof message.progress === 'number'" class="progress-label">{{ message.progress }}%</span>
      </div>

      <div v-if="message.content" class="md-body" v-html="html"></div>
      <span v-if="message.status === 'loading' && message.content" class="caret"></span>

      <p v-if="showThinking" class="thinking">正在思考…</p>

      <DiagnosisCard v-if="message.diagnosis" :diagnosis="message.diagnosis" />

      <ClarifyCard v-if="message.clarify" :clarify="message.clarify" />

      <SourceList v-if="hasSources" :sources="message.sources" />

      <div v-if="message.status === 'error'" class="error-box">
        <span class="error-title">生成失败</span>
        <span class="error-text">{{ message.error || '请求出现异常，请重试' }}</span>
        <el-button
          v-if="message.retryAvailable"
          class="retry-button"
          type="danger"
          plain
          size="small"
          @click="emit('retry')"
        >
          重试分析
        </el-button>
      </div>
      <p v-else-if="message.status === 'stopped'" class="stopped-tip">已停止生成</p>
    </div>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  width: 100%;
  justify-content: flex-start;
}

.assistant-card {
  position: relative;
  max-width: min(86%, 860px);
  padding: 16px 18px;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-shadow);
}

.assistant-card.is-error {
  border-color: var(--danger-border);
  background: #fffdfd;
}

.node-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
  color: var(--brand);
  font-size: 12.5px;
  font-weight: 500;
}

.progress-label {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.node-spinner {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 2px solid var(--brand-border);
  border-top-color: var(--brand);
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.caret {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--brand);
  border-radius: 1px;
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.thinking {
  color: var(--text-muted);
  font-size: 13.5px;
}

.error-box {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.error-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--danger);
}

.error-text {
  font-size: 12.5px;
  color: var(--danger-text);
  word-break: break-word;
}

.retry-button {
  align-self: flex-start;
  margin-top: 5px;
}

.stopped-tip {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted);
}

@media (max-width: 640px) {
  .assistant-card {
    max-width: 100%;
    padding: 14px 14px;
  }
}
</style>
