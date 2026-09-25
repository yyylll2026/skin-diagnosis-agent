<script setup>
import { computed, nextTick, ref } from 'vue'
import AssistantMessage from './AssistantMessage.vue'
import ChatComposer from './ChatComposer.vue'
import ChatWelcome from './ChatWelcome.vue'
import UserMessage from './UserMessage.vue'
import { useAgentChat } from '../../composables/useAgentChat'
import { useSessions } from '../../composables/useSessions'
import { useSidebar } from '../../composables/useSidebar'

/**
 * 对话面板：只负责「组装」——
 * 消息列表渲染、滚动调度、输入区接线。
 * 流式发送 / 停止逻辑在 useAgentChat，单条消息渲染在 UserMessage / AssistantMessage。
 */
const { currentSession, currentMessages, createSession } = useSessions()
const { isMobile, openMobile } = useSidebar()

const input = ref('')
const image = ref(null)
const useKnowledge = ref(true)

const scrollRef = ref(null)
let scrollScheduled = false

const messages = computed(() => currentMessages.value || [])
const sessionTitle = computed(() => currentSession.value?.title || '新会话')

async function scrollToBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

/** rAF 合帧：流式期间高频调用只触发一次实际滚动 */
function scheduleScroll() {
  if (scrollScheduled) return
  scrollScheduled = true
  requestAnimationFrame(() => {
    scrollScheduled = false
    scrollToBottom()
  })
}

const { generating, send, stop } = useAgentChat({
  input,
  image,
  useKnowledge,
  scheduleScroll,
})

function onNewSession() {
  stop()
  createSession()
}
</script>

<template>
  <section class="chat">
    <header class="chat-header">
      <div class="header-left">
        <button
          v-if="isMobile"
          class="menu-btn"
          type="button"
          title="打开会话列表"
          aria-label="打开会话列表"
          @click="openMobile"
        >
          ☰
        </button>
        <div class="header-text">
          <h2 class="header-title">{{ sessionTitle }}</h2>
          <p class="header-sub">多模态皮肤图像分析与健康咨询</p>
        </div>
      </div>
      <el-button text @click="onNewSession">切换为新会话</el-button>
    </header>

    <div ref="scrollRef" class="messages">
      <ChatWelcome v-if="!messages.length" />

      <template v-for="msg in messages" :key="msg.id">
        <UserMessage v-if="msg.role === 'user'" :message="msg" />
        <AssistantMessage v-else :message="msg" />
      </template>
    </div>

    <ChatComposer
      v-model:input="input"
      v-model:image="image"
      v-model:use-knowledge="useKnowledge"
      :generating="generating"
      @send="send"
      @stop="stop"
    />
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

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.header-text {
  min-width: 0;
}

.header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.header-sub {
  font-size: 12px;
  color: var(--text-muted);
}

.menu-btn {
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--card-bg);
  color: var(--text-secondary);
  font-size: 15px;
  cursor: pointer;
  transition: all 0.15s;
}

.menu-btn:hover {
  color: var(--brand);
  border-color: var(--brand-border);
  background: var(--brand-weak);
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

@media (max-width: 640px) {
  .chat-header {
    padding: 10px 14px;
  }

  .header-sub {
    display: none;
  }

  .messages {
    padding: 16px 12px 8px;
    gap: 14px;
  }
}
</style>
