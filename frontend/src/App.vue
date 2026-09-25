<script setup>
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AppSidebar from './components/layout/AppSidebar.vue'
import ChatPanel from './components/chat/ChatPanel.vue'
import LoginDialog from './components/auth/LoginDialog.vue'
import { useAuth } from './composables/useAuth'
import { useHealth } from './composables/useHealth'
import { useSessions } from './composables/useSessions'
import { useSidebar } from './composables/useSidebar'

// 管理面板按首开懒加载：普通用户永远用不到，不进首屏 chunk
const KnowledgePanel = defineAsyncComponent(() => import('./components/panels/KnowledgePanel.vue'))
const UserPanel = defineAsyncComponent(() => import('./components/panels/UserPanel.vue'))

const { init: initSessions } = useSessions()

const {
  authEnabled,
  ready: authReady,
  isLoggedIn,
  requireLogin,
  bootstrap: bootstrapAuth,
  installUnauthorizedHandler,
} = useAuth()

const { startPolling, stopPolling } = useHealth()
const { isMobile, collapsed, mobileOpen, initSidebar, closeMobile } = useSidebar()

initSessions()
initSidebar()
// 先装 401 处理器，再探测登录态：否则启动阶段的请求若返回 401，
// 会因为处理器尚未注册而被静默吞掉，界面停在半死状态
installUnauthorizedHandler()

const knowledgeOpen = ref(false)
const userPanelOpen = ref(false)
// 首次打开后才真正挂载（并加载）对应面板 chunk
const knowledgeMounted = ref(false)
const userPanelMounted = ref(false)
watch(knowledgeOpen, (open) => {
  if (open) knowledgeMounted.value = true
})
watch(userPanelOpen, (open) => {
  if (open) userPanelMounted.value = true
})

/**
 * 是否允许使用对话功能。
 *
 * 鉴权关闭时（本地开发 / 单机部署）恒为真；开启时必须已登录。
 * 这只是**界面门禁**——服务端每个接口都会独立校验身份与权限。
 */
const canUseApp = computed(() => !authEnabled.value || isLoggedIn.value)

onMounted(async () => {
  // 先探测登录态（内部会读 /system/health 拿鉴权开关），再刷新 Ollama 状态灯。
  // 顺序反过来的话，未登录时 refreshHealth 会先吃一个 401。
  await bootstrapAuth()
  startPolling(30000)
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<template>
  <div class="app-shell">
    <aside
      class="sidebar"
      :class="{
        'is-collapsed': !isMobile && collapsed,
        'is-mobile': isMobile,
        'is-mobile-open': isMobile && mobileOpen,
      }"
    >
      <AppSidebar @open-knowledge="knowledgeOpen = true" @open-users="userPanelOpen = true" />
    </aside>

    <!-- 移动端侧栏遮罩 -->
    <div
      v-if="isMobile && mobileOpen"
      class="backdrop"
      aria-hidden="true"
      @click="closeMobile"
    ></div>

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

    <KnowledgePanel v-if="knowledgeMounted" v-model="knowledgeOpen" />
    <UserPanel v-if="userPanelMounted" v-model="userPanelOpen" />
    <LoginDialog />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  width: 100%;
  overflow: hidden;
  background: var(--bg);
}

/* ---------------------------- 左侧栏 ---------------------------- */
.sidebar {
  width: var(--sidebar-width);
  flex: 0 0 var(--sidebar-width);
  padding: 16px 14px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  transition: width 0.2s ease, flex-basis 0.2s ease, padding 0.2s ease, transform 0.25s ease;
  overflow: hidden;
}

.sidebar.is-collapsed {
  width: var(--sidebar-width-collapsed);
  flex-basis: var(--sidebar-width-collapsed);
  padding: 16px 8px;
}

/* ---------------------------- 移动端：侧栏变浮层抽屉 ---------------------------- */
.sidebar.is-mobile {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 60;
  width: min(var(--sidebar-width), 84vw);
  flex-basis: auto;
  box-shadow: 8px 0 32px rgba(15, 23, 42, 0.14);
  transform: translateX(-105%);
}

.sidebar.is-mobile-open {
  transform: translateX(0);
}

.backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(15, 23, 42, 0.4);
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
