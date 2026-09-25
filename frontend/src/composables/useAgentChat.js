import { onBeforeUnmount, ref, watch } from 'vue'
import { streamAgentChat } from '../api'
import { nodeMeta } from '../constants/agent'
import { uid } from '../utils/common'
import { resolveClarify, resolveDiagnosis, resolveNode, resolveSources } from '../utils/stream'
import { useSessions } from './useSessions'

/**
 * 智能体流式对话的发送 / 停止逻辑。
 *
 * 从原 ChatPanel 中抽出：视图（消息列表、输入区）只负责渲染与滚动，
 * 这里负责把一轮问答写进当前会话的消息数组，并处理 SSE 事件归一。
 *
 * @param {object} options
 * @param {Ref<string>} options.input 输入框文本（发送后会被清空）
 * @param {Ref<object|null>} options.image 已选图片（发送后会被清空）
 * @param {Ref<boolean>} options.useKnowledge 是否启用知识库检索
 * @param {() => void} options.scheduleScroll 视图层注入的滚动调度
 */
export function useAgentChat({ input, image, useKnowledge, scheduleScroll }) {
  const {
    currentId,
    currentSession,
    createSession,
    maybeRenameFromMessage,
    touchSession,
  } = useSessions()

  const generating = ref(false)

  let abortController = null
  let stopped = false

  async function send() {
    const messageText = (input.value || '').trim()
    const currentImage = image.value || null
    if (generating.value || (!messageText && !currentImage)) return

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
    scheduleScroll?.()

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
            const meta = nodeMeta(key)
            reply.nodeLabel = meta.label
            reply.nodeText = meta.active
            scheduleScroll?.()
          },
          onVision: (data) => {
            const diagnosis = resolveDiagnosis(data)
            if (diagnosis) reply.diagnosis = diagnosis
            scheduleScroll?.()
          },
          onClarify: (data) => {
            const clarify = resolveClarify(data)
            if (clarify) reply.clarify = clarify
            scheduleScroll?.()
          },
          onSources: (data) => {
            const list = resolveSources(data)
            if (list.length) reply.sources = list
            scheduleScroll?.()
          },
          onToken: (text) => {
            reply.content += text
            scheduleScroll?.()
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
      scheduleScroll?.()
    }
  }

  function stop() {
    if (!generating.value) return
    stopped = true
    if (abortController) abortController.abort()
  }

  // 切换会话时中断仍在进行的生成
  watch(currentId, () => {
    if (generating.value) stop()
  })

  onBeforeUnmount(() => {
    if (abortController) abortController.abort()
  })

  return { generating, send, stop }
}
