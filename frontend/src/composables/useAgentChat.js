import { onBeforeUnmount, ref, watch } from 'vue'
import {
  cancelAnalysisTask,
  createAnalysisTask,
  getAnalysisTask,
  retryAnalysisTask,
  streamAgentChat,
} from '../api'
import { nodeMeta } from '../constants/agent'
import { uid } from '../utils/common'
import { resolveClarify, resolveDiagnosis, resolveNode, resolveSources } from '../utils/stream'
import { useSessions } from './useSessions'

/**
 * 智能体对话的发送 / 停止逻辑。
 * 纯文本沿用 SSE token 流；带图请求提交为后台任务并轮询进度，避免长时间 HTTP 连接超时。
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
  let activeTaskId = ''
  let stopped = false

  function applyTaskResult(reply, result) {
    reply.content = String(result?.answer || '')
    reply.diagnosis = resolveDiagnosis(result?.vision)
    reply.clarify = resolveClarify(result?.clarify)
    reply.sources = resolveSources(result?.sources)
    reply.progress = 100
    reply.nodeLabel = '分析完成'
    reply.nodeText = ''
  }

  function finish(reply, session, status) {
    if (reply.status !== 'loading') return
    reply.status = stopped && status !== 'error' ? 'stopped' : status
    generating.value = false
    abortController = null
    activeTaskId = ''
    touchSession(session)
    scheduleScroll?.()
  }

  async function monitorTask(taskId, reply, session) {
    activeTaskId = taskId
    while (generating.value) {
      if (stopped) {
        try {
          await cancelAnalysisTask(taskId)
        } catch (err) {
          // 任务可能已完成；前端仍以本地停止状态收尾
        }
        finish(reply, session, 'stopped')
        return
      }

      const task = await getAnalysisTask(taskId)
      reply.taskId = task.task_id
      reply.progress = task.progress
      reply.retryAvailable = !!task.retry_available
      reply.nodeText = task.current_step || (task.status === 'queued' ? '排队中' : '正在分析')
      reply.nodeLabel = reply.nodeText
      scheduleScroll?.()

      if (task.status === 'succeeded') {
        applyTaskResult(reply, task.result)
        finish(reply, session, 'done')
        return
      }
      if (task.status === 'failed') {
        reply.error = task.error || '后台分析失败'
        finish(reply, session, 'error')
        return
      }
      if (task.status === 'cancelled') {
        stopped = true
        finish(reply, session, 'stopped')
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 1200))
    }
  }

  async function runBackgroundImageTask(payload, reply, session) {
    const task = await createAnalysisTask(payload)
    reply.taskId = task.task_id
    reply.progress = task.progress
    reply.nodeText = task.current_step || '任务已提交'
    // 先持久化任务 ID，不持久化图片；刷新页面后可继续查询这项任务。
    touchSession(session)
    await monitorTask(task.task_id, reply, session)
  }

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
      hasImage: !!currentImage,
      createdAt: Date.now(),
    })
    maybeRenameFromMessage(session, messageText || '图片咨询')

    session.messages.push({
      id: uid(),
      role: 'assistant',
      content: '',
      nodeLabel: '',
      nodeText: '',
      progress: null,
      taskId: '',
      retryAvailable: false,
      diagnosis: null,
      clarify: null,
      sources: [],
      status: 'loading',
      error: '',
      createdAt: Date.now(),
    })
    const reply = session.messages[session.messages.length - 1]

    input.value = ''
    image.value = null
    stopped = false
    generating.value = true
    abortController = new AbortController()
    scheduleScroll?.()

    try {
      if (currentImage) {
        await runBackgroundImageTask(
          {
            session_id: session.id,
            message: messageText,
            image_base64: currentImage.base64,
            image_mime: currentImage.mime,
            use_knowledge: useKnowledge.value,
          },
          reply,
          session
        )
      } else {
        await streamAgentChat(
          { session_id: session.id, message: messageText, use_knowledge: useKnowledge.value },
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
              const clarify = resolveClarify(data)
              if (clarify) reply.clarify = clarify
              finish(reply, session, 'done')
            },
            onError: (err) => {
              reply.error = err?.message || '生成失败，请重试'
              finish(reply, session, 'error')
            },
          },
          abortController?.signal
        )
      }
    } catch (err) {
      if (reply.status === 'loading') {
        reply.error = err?.message || '生成失败，请重试'
        finish(reply, session, 'error')
      }
    } finally {
      if (reply.status === 'loading') finish(reply, session, stopped ? 'stopped' : 'done')
    }
  }

  async function retryTask(reply) {
    if (generating.value || !reply?.taskId || !reply.retryAvailable) return
    const session = currentSession.value
    if (!session) return
    reply.status = 'loading'
    reply.error = ''
    reply.retryAvailable = false
    reply.progress = 0
    reply.nodeText = '正在重新排队'
    stopped = false
    generating.value = true
    scheduleScroll?.()
    try {
      const task = await retryAnalysisTask(reply.taskId)
      await monitorTask(task.task_id, reply, session)
    } catch (err) {
      reply.error = err?.message || '无法重试，请重新上传图片'
      reply.retryAvailable = false
      finish(reply, session, 'error')
    }
  }

  function stop() {
    if (!generating.value) return
    stopped = true
    if (activeTaskId) {
      const taskId = activeTaskId
      activeTaskId = ''
      cancelAnalysisTask(taskId).catch(() => {})
    } else if (abortController) {
      abortController.abort()
    }
  }

  watch(currentId, () => {
    if (generating.value) stop()
  })

  onBeforeUnmount(() => {
    if (generating.value) stop()
    if (abortController) abortController.abort()
  })

  // 刷新页面后从本地会话恢复 task_id，并继续轮询服务端持久化状态。
  const pendingReply = [...(currentSession.value?.messages || [])]
    .reverse()
    .find((message) => message.role === 'assistant' && message.status === 'loading' && message.taskId)
  if (pendingReply) {
    generating.value = true
    monitorTask(pendingReply.taskId, pendingReply, currentSession.value).catch((err) => {
      pendingReply.error = err?.message || '无法恢复后台任务状态'
      finish(pendingReply, currentSession.value, 'error')
    })
  }

  return { generating, send, stop, retryTask }
}
