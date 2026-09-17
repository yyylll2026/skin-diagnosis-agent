<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import ImageUploader from './ImageUploader.vue'
import DiagnosisCard from './DiagnosisCard.vue'
import ClarifyCard from './ClarifyCard.vue'
import SourceList from './SourceList.vue'
import { streamAgentChat } from '../api'
import { renderMarkdown } from '../utils/markdown'
import { useSessions } from '../composables/useSessions'

const { currentId, currentSession, currentMessages, createSession, maybeRenameFromMessage, touchSession } =
  useSessions()

const NODES = {
  preprocess: { label: '预处理输入', active: '正在预处理输入…' },
  route: { label: '意图识别', active: '正在识别意图…' },
  vision_analyze: { label: '图像分析', active: '正在分析图像…' },
  check_completeness: { label: '信息完备性判断', active: '正在判断信息是否充分…' },
  clarify: { label: '生成追问', active: '正在整理需要补充的信息…' },
  build_query: { label: '构建检索意图', active: '正在构建检索意图…' },
  retrieve: { label: '检索知识库', active: '正在检索知识库…' },
  generate: { label: '生成回答', active: '正在生成回答…' },
  safety_review: { label: '安全审查', active: '正在进行安全审查…' },
}

const input = ref('')
const image = ref(null)
const useKnowledge = ref(true)
const generating = ref(false)

const scrollRef = ref(null)
let abortController = null
let stopped = false
let scrollScheduled = false

const messages = computed(() => currentMessages.value || [])
const canSend = computed(() => !generating.value && (!!input.value.trim() || !!image.value))
const sessionTitle = computed(() => currentSession.value?.title || '新会话')

function uid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

async function scrollToBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function scheduleScroll() {
  if (scrollScheduled) return
  scrollScheduled = true
  requestAnimationFrame(() => {
    scrollScheduled = false
    scrollToBottom()
  })
}

/* ---------------- 事件字段容错解析 ---------------- */

function resolveNode(data) {
  if (!data) return ''
  if (typeof data === 'string') return data
  return data.node || data.name || data.step || data.current || ''
}

function resolveDiagnosis(data) {
  if (!data || typeof data !== 'object') return null
  if (data.diagnosis && typeof data.diagnosis === 'object') return data.diagnosis
  if (data.result && typeof data.result === 'object') return data.result
  if ('risk_level' in data || 'hypotheses' in data || 'summary' in data) return data
  if (data.data && typeof data.data === 'object') return data.data
  return null
}

function resolveSources(data) {
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.sources)) return data.sources
  if (Array.isArray(data?.data)) return data.data
  if (Array.isArray(data?.items)) return data.items
  return []
}

function resolveClarify(data) {
  if (!data || typeof data !== 'object') return null
  if (Array.isArray(data.missing) || Array.isArray(data.missing_keys)) return data
  if (data.data && typeof data.data === 'object') return data.data
  // 兜底：后端把判定结果放在 done 事件的 clarify 字段里
  if (data.clarify && typeof data.clarify === 'object') return data.clarify
  return null
}

/* ---------------- 发送 / 停止 ---------------- */

function onKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    send()
  }
}

async function send() {
  if (!canSend.value) return

  const messageText = input.value.trim()
  const currentImage = image.value

  let session = currentSession.value
  if (!session) {
    createSession()
    session = currentSession.value
  }
  if (!Array.isArray(session.messages)) session.messages = []

  session.messages.push({
    id: uid(),
    role: 'user',
    content: messageText,
    image: currentImage?.dataUrl || '',
    createdAt: Date.now(),
  })
  maybeRenameFromMessage(session, messageText || '图片咨询')

  session.messages.push({
    id: uid(),
    role: 'assistant',
    content: '',
    nodeLabel: '',
    nodeText: '',
    diagnosis: null,
    clarify: null,
    sources: [],
    status: 'loading',
    error: '',
    createdAt: Date.now(),
  })
  // 取回响应式代理，直接修改原始对象不会触发视图更新
  const reply = session.messages[session.messages.length - 1]

  input.value = ''
  image.value = null
  stopped = false
  generating.value = true
  abortController = new AbortController()
  scheduleScroll()

  const finish = (status) => {
    if (reply.status !== 'loading') return
    reply.status = stopped ? 'stopped' : status
    generating.value = false
    abortController = null
    touchSession(session)
  }

  try {
    await streamAgentChat(
      {
        session_id: session.id,
        message: messageText,
        image_base64: currentImage?.base64 || null,
        image_mime: currentImage?.mime || null,
        use_knowledge: useKnowledge.value,
      },
      {
        onNode: (data) => {
          const key = resolveNode(data)
          if (!key) return
          const meta = NODES[key] || { label: key, active: `正在${key}…` }
          reply.nodeLabel = meta.label
          reply.nodeText = meta.active
          scheduleScroll()
        },
        onVision: (data) => {
          const diagnosis = resolveDiagnosis(data)
          if (diagnosis) reply.diagnosis = diagnosis
          scheduleScroll()
        },
        onClarify: (data) => {
          const clarify = resolveClarify(data)
          if (clarify) reply.clarify = clarify
          scheduleScroll()
        },
        onSources: (data) => {
          const list = resolveSources(data)
          if (list.length) reply.sources = list
          scheduleScroll()
        },
        onToken: (text) => {
          reply.content += text
          scheduleScroll()
        },
        onDone: (data) => {
          // done 事件里也带一份 clarify：若中途的 clarify 事件丢失，
          // 这里仍能补上结构化信息，卡片不会凭空消失
          const clarify = resolveClarify(data)
          if (clarify) reply.clarify = clarify
          finish('done')
        },
        onError: (err) => {
          reply.error = err?.message || '生成失败，请重试'
          finish('error')
        },
      },
      abortController?.signal
    )
  } catch (err) {
    reply.error = err?.message || '生成失败，请重试'
    finish('error')
  } finally {
    finish('done')
    scheduleScroll()
  }
}

function stop() {
  if (!generating.value) return
  stopped = true
  if (abortController) abortController.abort()
}

function onNewSession() {
  stop()
  createSession()
}

// 切换会话时中断仍在进行的生成
watch(currentId, () => {
  if (generating.value) stop()
})

onBeforeUnmount(() => {
  if (abortController) abortController.abort()
})
</script>

<template>
  <section class="chat">
    <header class="chat-header">
      <div class="header-left">
        <h2 class="header-title">{{ sessionTitle }}</h2>
        <p class="header-sub">多模态皮肤图像分析与健康咨询</p>
      </div>
      <el-button text @click="onNewSession">切换为新会话</el-button>
    </header>

    <div ref="scrollRef" class="messages">
      <div v-if="!messages.length" class="welcome">
        <div class="welcome-logo">皮</div>
        <h3 class="welcome-title">皮肤诊断智能体</h3>
        <p class="welcome-text">
          上传一张皮肤图像，或直接描述症状（部位、形态、持续时间等），我会给出初步的视觉观察与就医建议。
        </p>
        <div class="welcome-tips">
          <span class="tip">支持点击 / 拖拽 / 粘贴图片</span>
          <span class="tip">可开启知识库检索获取参考资料</span>
          <span class="tip">结果仅供辅助参考</span>
        </div>
      </div>

      <div v-for="msg in messages" :key="msg.id" class="row" :class="`row-${msg.role}`">
        <template v-if="msg.role === 'user'">
          <div class="user-stack">
            <img v-if="msg.image" class="user-image" :src="msg.image" alt="上传的皮肤图像" />
            <div v-if="msg.content" class="user-bubble">{{ msg.content }}</div>
          </div>
        </template>

        <template v-else>
          <div class="assistant-card" :class="{ 'is-error': msg.status === 'error' }">
            <div v-if="msg.status === 'loading' && msg.nodeText" class="node-status">
              <span class="node-spinner"></span>
              <span>{{ msg.nodeText }}</span>
            </div>

            <div v-if="msg.content" class="md-body" v-html="renderMarkdown(msg.content)"></div>
            <span v-if="msg.status === 'loading' && msg.content" class="caret"></span>

            <p v-if="msg.status === 'loading' && !msg.content && !msg.nodeText" class="thinking">
              正在思考…
            </p>

            <DiagnosisCard v-if="msg.diagnosis" :diagnosis="msg.diagnosis" />

            <ClarifyCard v-if="msg.clarify" :clarify="msg.clarify" />

            <SourceList v-if="Array.isArray(msg.sources) && msg.sources.length" :sources="msg.sources" />

            <div v-if="msg.status === 'error'" class="error-box">
              <span class="error-title">生成失败</span>
              <span class="error-text">{{ msg.error || '请求出现异常，请重试' }}</span>
            </div>
            <p v-else-if="msg.status === 'stopped'" class="stopped-tip">已停止生成</p>
          </div>
        </template>
      </div>
    </div>

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
            <el-button v-if="generating" type="danger" plain @click="stop">停止生成</el-button>
            <el-button v-else type="primary" :disabled="!canSend" @click="send">发送</el-button>
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
  </section>
</template>

<style scoped>
.chat {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}

/* ------------------------------ 顶部 ------------------------------ */
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 24px;
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
}

.header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.35;
}

.header-sub {
  font-size: 12px;
  color: var(--text-muted);
}

/* ------------------------------ 消息区 ------------------------------ */
.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 24px 8px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.row {
  display: flex;
  width: 100%;
}

.row-user {
  justify-content: flex-end;
}

.row-assistant {
  justify-content: flex-start;
}

/* 用户气泡 */
.user-stack {
  max-width: min(72%, 620px);
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}

.user-image {
  max-width: 220px;
  max-height: 220px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  box-shadow: var(--card-shadow);
  object-fit: cover;
}

.user-bubble {
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  background: var(--brand);
  color: var(--text-inverse);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: 0 2px 8px rgba(14, 116, 144, 0.18);
}

/* 助手卡片 */
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
  color: #991b1b;
  word-break: break-word;
}

.stopped-tip {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted);
}

/* ------------------------------ 欢迎页 ------------------------------ */
.welcome {
  margin: auto;
  max-width: 520px;
  padding: 32px 24px;
  text-align: center;
}

.welcome-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 16px;
  border-radius: 16px;
  background: var(--brand);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 600;
}

.welcome-title {
  font-size: 19px;
  font-weight: 600;
  color: var(--text);
}

.welcome-text {
  margin-top: 8px;
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text-secondary);
}

.welcome-tips {
  margin-top: 18px;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}

.tip {
  padding: 4px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
  font-size: 12px;
  color: var(--text-secondary);
}

/* ------------------------------ 输入区 ------------------------------ */
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
</style>
