<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ChatPanel from './components/ChatPanel.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import LoginDialog from './components/LoginDialog.vue'
import UserPanel from './components/UserPanel.vue'
import { getHealth } from './api'
import { useAuth } from './composables/useAuth'
import { useSessions } from './composables/useSessions'

const {
  sessions,
  currentId,
  init: initSessions,
  createSession,
  selectSession,
  removeSession,
} = useSessions()

const {
  authEnabled,
  ready: authReady,
  isLoggedIn,
  isAdmin,
  displayName,
  can,
  requireLogin,
  bootstrap: bootstrapAuth,
  installUnauthorizedHandler,
  logout,
} = useAuth()

initSessions()
// 先装 401 处理器，再探测登录态：否则启动阶段的请求若返回 401，
// 会因为处理器尚未注册而被静默吞掉，界面停在半死状态
installUnauthorizedHandler()

const collapsed = ref(false)
const knowledgeOpen = ref(false)
const userPanelOpen = ref(false)

const health = ref({ online: false, model: '', checked: false })
const healthLoading = ref(false)
let timer = null

/**
 * 是否允许使用对话功能。
 *
 * 鉴权关闭时（本地开发 / 单机部署）恒为真；开启时必须已登录。
 * 这只是**界面门禁**——服务端每个接口都会独立校验身份与权限。
 */
const canUseApp = computed(() => !authEnabled.value || isLoggedIn.value)

/** 知识库管理入口：需要写权限，客户不显示 */
const canManageKnowledge = computed(() => can('kb:write') || can('kb:reindex'))

/** 用户管理入口：仅管理员 */
const canManageUsers = computed(() => isAdmin.value)

async function onLogout() {
  await logout()
  // 清掉本地会话列表，避免下一个登录者看到上一个人的对话标题
  sessions.value = []
  createSession()
}

function firstBoolean(source, keys) {
  if (!source || typeof source !== 'object') return undefined
  for (const key of keys) {
    if (typeof source[key] === 'boolean') return source[key]
  }
  return undefined
}

function pickModel(source) {
  if (!source || typeof source !== 'object') return ''
  const keys = ['model', 'current_model', 'llm_model', 'ollama_model', 'vision_model', 'name']
  for (const key of keys) {
    if (typeof source[key] === 'string' && source[key]) return source[key]
  }
  return ''
}

async function refreshHealth() {
  if (healthLoading.value) return
  healthLoading.value = true
  try {
    const data = await getHealth()
    const onlineFlag =
      firstBoolean(data, ['ollama', 'ollama_connected', 'ollama_available', 'healthy', 'ok']) ??
      ['ok', 'healthy', 'up'].includes(String(data?.status || '').toLowerCase())
    // 后端也可能把 ollama 状态放在嵌套字段里
    const nested = data?.ollama && typeof data.ollama === 'object' ? data.ollama : null
    const nestedFlag = firstBoolean(nested, ['connected', 'available', 'ok', 'running'])
    health.value = {
      online: nestedFlag ?? onlineFlag,
      model: pickModel(data) || pickModel(nested),
      checked: true,
    }
  } catch (err) {
    health.value = { online: false, model: '', checked: true }
  } finally {
    healthLoading.value = false
  }
}

function onNewSession() {
  createSession()
}

function onSelectSession(id) {
  selectSession(id)
}

function onRemoveSession(id) {
  removeSession(id)
}

onMounted(async () => {
  // 先探测登录态（内部会读 /system/health 拿鉴权开关），再刷新 Ollama 状态灯。
  // 顺序反过来的话，未登录时 refreshHealth 会先吃一个 401。
  await bootstrapAuth()
  refreshHealth()
  timer = window.setInterval(refreshHealth, 30000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ 'is-collapsed': collapsed }">
      <div class="brand">
        <div class="brand-logo">皮</div>
        <div v-show="!collapsed" class="brand-meta">
          <h1 class="brand-title">皮肤诊断智能体</h1>
          <p class="brand-sub">本地部署 · Qwen2.5-VL</p>
        </div>
        <button
          class="collapse-btn"
          type="button"
          :title="collapsed ? '展开侧栏' : '收起侧栏'"
          @click="collapsed = !collapsed"
        >
          {{ collapsed ? '›' : '‹' }}
        </button>
      </div>

      <el-button v-if="!collapsed" class="new-session" type="primary" @click="onNewSession">
        新建会话
      </el-button>
      <el-button
        v-else
        class="new-session-mini"
        type="primary"
        circle
        title="新建会话"
        @click="onNewSession"
      >
        ＋
      </el-button>

      <div v-show="!collapsed" class="session-block">
        <div class="block-label">历史会话</div>
        <div class="session-list">
          <button
            v-for="item in sessions"
            :key="item.id"
            type="button"
            class="session-item"
            :class="{ active: item.id === currentId }"
            @click="onSelectSession(item.id)"
          >
            <span class="session-title">{{ item.title }}</span>
            <span
              class="session-del"
              role="button"
              title="删除会话"
              @click.stop="onRemoveSession(item.id)"
              >×</span
            >
          </button>
          <p v-if="!sessions.length" class="empty-tip">暂无会话</p>
        </div>
      </div>

      <div v-show="!collapsed" class="sidebar-bottom">
        <!-- 入口按权限显隐。注意这只是体验优化：
             客户即使手工调用 /kb/documents 也会在服务端拿到 403 -->
        <el-button
          v-if="canManageKnowledge"
          class="kb-entry"
          plain
          @click="knowledgeOpen = true"
        >
          知识库管理
        </el-button>
        <el-button v-if="canManageUsers" class="kb-entry" plain @click="userPanelOpen = true">
          用户与审计
        </el-button>

        <!-- 当前登录用户（鉴权开启时才显示） -->
        <div v-if="authEnabled" class="account">
          <div class="account-text">
            <p class="account-name" :title="displayName">
              {{ displayName || '未登录' }}
            </p>
            <p class="account-role">{{ isAdmin ? '管理员' : '客户' }}</p>
          </div>
          <el-button v-if="isLoggedIn" link size="small" @click="onLogout">登出</el-button>
          <el-button v-else link size="small" @click="requireLogin()">登录</el-button>
        </div>

        <div class="health">
          <span class="health-dot" :class="health.online ? 'is-ok' : 'is-bad'"></span>
          <div class="health-text">
            <p class="health-line">
              {{ health.checked ? (health.online ? 'Ollama 已连接' : 'Ollama 未连接') : '检测中…' }}
            </p>
            <p class="health-model" :title="health.model">模型：{{ health.model || '—' }}</p>
          </div>
          <button class="health-refresh" type="button" title="刷新状态" @click="refreshHealth">
            ⟳
          </button>
        </div>
      </div>

      <button
        v-if="collapsed"
        class="collapsed-health"
        type="button"
        :class="health.online ? 'is-ok' : 'is-bad'"
        :title="health.online ? 'Ollama 已连接' : 'Ollama 未连接'"
        @click="refreshHealth"
      ></button>
    </aside>

    <main class="main">
      <!-- 未登录时不挂载 ChatPanel：既避免它发请求吃 401，
           也避免用户看到一个可以打字但发不出去的界面 -->
      <ChatPanel v-if="canUseApp" />
      <div v-else class="gate">
        <p class="gate-title">请先登录</p>
        <p class="gate-text">本系统需要登录后使用。客户账号可提问并只读查看知识库。</p>
        <el-button v-if="authReady" type="primary" @click="requireLogin()">登录 / 注册</el-button>
      </div>
    </main>

    <KnowledgePanel v-model="knowledgeOpen" />
    <UserPanel v-model="userPanelOpen" />
    <LoginDialog />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  width: 100%;
  overflow: hidden;
  background: var(--bg);
}

/* ---------------------------- 左侧栏 ---------------------------- */
.sidebar {
  width: var(--sidebar-width);
  flex: 0 0 var(--sidebar-width);
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 14px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  transition: width 0.2s ease, flex-basis 0.2s ease, padding 0.2s ease;
  overflow: hidden;
}

.sidebar.is-collapsed {
  width: var(--sidebar-width-collapsed);
  flex-basis: var(--sidebar-width-collapsed);
  padding: 16px 8px;
  align-items: center;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
}

.sidebar.is-collapsed .brand {
  flex-direction: column;
  gap: 8px;
}

.brand-logo {
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  border-radius: 11px;
  background: var(--brand);
  color: var(--text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.brand-meta {
  min-width: 0;
  flex: 1;
}

.brand-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.3;
  white-space: nowrap;
}

.brand-sub {
  font-size: 11.5px;
  color: var(--text-muted);
  white-space: nowrap;
}

.collapse-btn {
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-secondary);
  border-radius: 6px;
  cursor: pointer;
  line-height: 1;
  font-size: 13px;
  transition: all 0.15s;
}

.collapse-btn:hover {
  color: var(--brand);
  border-color: var(--brand-border);
  background: var(--brand-weak);
}

.new-session {
  width: 100%;
  height: 38px;
}

.new-session-mini {
  width: 38px;
  height: 38px;
  font-size: 16px;
}

.session-block {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.block-label {
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 8px;
  padding-left: 2px;
}

.session-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-right: 2px;
}

.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.session-item:hover {
  background: #f1f5f9;
  color: var(--text);
}

.session-item.active {
  background: var(--brand-weak);
  border-color: var(--brand-border);
  color: var(--brand);
  font-weight: 500;
}

.session-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-del {
  opacity: 0;
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-size: 15px;
  line-height: 1;
  color: var(--text-muted);
  transition: opacity 0.15s, background 0.15s, color 0.15s;
}

.session-item:hover .session-del {
  opacity: 1;
}

.session-del:hover {
  background: var(--danger-bg);
  color: var(--danger);
}

.empty-tip {
  padding: 14px 6px;
  font-size: 12.5px;
  color: var(--text-muted);
  text-align: center;
}

/* ---------------------------- 底部状态 ---------------------------- */
.sidebar-bottom {
  border-top: 1px solid var(--divider);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.kb-entry {
  width: 100%;
}

.health {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: #f8fafc;
  border: 1px solid var(--border);
}

.health-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 8px;
  border-radius: 50%;
  background: var(--text-muted);
}

.health-dot.is-ok,
.collapsed-health.is-ok {
  background: var(--success);
  box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.14);
}

.health-dot.is-bad,
.collapsed-health.is-bad {
  background: var(--danger);
  box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.14);
}

.health-text {
  flex: 1;
  min-width: 0;
}

.health-line {
  font-size: 12.5px;
  color: var(--text);
  line-height: 1.4;
}

.health-model {
  font-size: 11.5px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.health-refresh {
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 2px;
  border-radius: 4px;
  transition: color 0.15s, transform 0.2s;
}

.health-refresh:hover {
  color: var(--brand);
  transform: rotate(90deg);
}

.collapsed-health {
  width: 10px;
  height: 10px;
  border: none;
  border-radius: 50%;
  padding: 0;
  cursor: pointer;
}

/* ---------------------------- 当前账号 ---------------------------- */
.account {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
}

.account-text {
  flex: 1;
  min-width: 0;
}

.account-name {
  font-size: 12.5px;
  color: var(--text);
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-role {
  font-size: 11.5px;
  color: var(--brand);
}

/* ---------------------------- 未登录门禁 ---------------------------- */
.gate {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 24px;
  text-align: center;
}

.gate-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}

.gate-text {
  font-size: 13px;
  color: var(--text-secondary);
  max-width: 340px;
  line-height: 1.6;
  margin-bottom: 4px;
}

/* ---------------------------- 右栏 ---------------------------- */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>
