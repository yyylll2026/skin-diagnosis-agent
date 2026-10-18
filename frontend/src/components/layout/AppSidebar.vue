<script setup>
import { computed } from 'vue'
import { useAuth } from '../../composables/useAuth'
import { useHealth } from '../../composables/useHealth'
import { useSessions } from '../../composables/useSessions'
import { useSidebar } from '../../composables/useSidebar'

/**
 * 应用侧栏：品牌区、会话列表、管理入口、当前账号与 Ollama 状态灯。
 *
 * 桌面端可收起为窄条；移动端（≤860px）作为浮层抽屉渲染（由 App.vue 控制容器类名）。
 */
const emit = defineEmits([
  'open-knowledge',
  'open-users',
  'open-health-records',
  'open-patients',
])

const { sessions, currentId, createSession, selectSession, removeSession } = useSessions()
const { authEnabled, isLoggedIn, displayName, roleLabel, can, requireLogin, logout } = useAuth()
const { health, refreshHealth } = useHealth()
const { isMobile, collapsed, toggleCollapsed, closeMobile } = useSidebar()

/** 知识库管理入口：需要写权限，客户不显示 */
const canManageKnowledge = computed(() => can('kb:write') || can('kb:reindex'))

/** 用户管理入口：仅管理员（服务端同样只给 admin 发 user:write 权限点） */
const canManageUsers = computed(() => can('user:write'))

/**
 * 患者工作台入口：与后端四个 /health-records/patients 端点同一个权限点。
 * 鉴权关闭时（本地开发以合成管理员进入）没有 permissions 数组，但服务端会把
 * 每个请求当作管理员，因此也显示入口。
 */
const canReviewPatients = computed(
  () => !authEnabled.value || can('health_records:read_all')
)

function onNewSession() {
  createSession()
  closeMobile()
}

function onSelectSession(id) {
  selectSession(id)
  closeMobile()
}

function onRemoveSession(id) {
  removeSession(id)
}

async function onLogout() {
  await logout()
  // 清掉本地会话列表，避免下一个登录者看到上一个人的对话标题
  sessions.value = []
  createSession()
}
</script>

<template>
  <div class="sidebar-inner" :class="{ 'is-collapsed': collapsed && !isMobile }">
    <div class="brand">
      <div class="brand-logo">皮</div>
      <div v-show="!collapsed" class="brand-meta">
        <h1 class="brand-title">皮肤诊断智能体</h1>
        <p class="brand-sub">本地部署 · Qwen2.5-VL</p>
      </div>
      <button
        v-if="!isMobile"
        class="collapse-btn"
        type="button"
        :title="collapsed ? '展开侧栏' : '收起侧栏'"
        :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
        @click="toggleCollapsed"
      >
        {{ collapsed ? '›' : '‹' }}
      </button>
      <button
        v-else
        class="collapse-btn"
        type="button"
        title="关闭侧栏"
        aria-label="关闭侧栏"
        @click="closeMobile"
      >
        ×
      </button>
    </div>

    <el-button v-if="!collapsed || isMobile" class="new-session" type="primary" @click="onNewSession">
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

    <div v-show="!collapsed || isMobile" class="session-block">
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
            tabindex="0"
            title="删除会话"
            @click.stop="onRemoveSession(item.id)"
            @keydown.enter.stop="onRemoveSession(item.id)"
            >×</span
          >
        </button>
        <p v-if="!sessions.length" class="empty-tip">暂无会话</p>
      </div>
    </div>

    <div v-show="!collapsed || isMobile" class="sidebar-bottom">
      <el-button
        v-if="!authEnabled || isLoggedIn"
        class="kb-entry"
        plain
        @click="emit('open-health-records')"
      >
        个人健康记录
      </el-button>

      <!-- 患者（医生）工作台：只读查看全部患者记录与趋势 -->
      <el-button v-if="canReviewPatients" class="kb-entry" plain @click="emit('open-patients')">
        患者工作台
      </el-button>

      <!-- 入口按权限显隐。注意这只是体验优化：
           客户即使手工调用 /kb/documents 也会在服务端拿到 403 -->
      <el-button v-if="canManageKnowledge" class="kb-entry" plain @click="emit('open-knowledge')">
        知识库管理
      </el-button>
      <el-button v-if="canManageUsers" class="kb-entry" plain @click="emit('open-users')">
        用户与审计
      </el-button>

      <!-- 当前登录用户（鉴权开启时才显示） -->
      <div v-if="authEnabled" class="account">
        <div class="account-text">
          <p class="account-name" :title="displayName">{{ displayName || '未登录' }}</p>
          <p class="account-role">{{ roleLabel || '未登录' }}</p>
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
        <button
          class="health-refresh"
          type="button"
          title="刷新状态"
          aria-label="刷新状态"
          @click="refreshHealth"
        >
          ⟳
        </button>
      </div>
    </div>

    <button
      v-if="collapsed && !isMobile"
      class="collapsed-health"
      type="button"
      :class="health.online ? 'is-ok' : 'is-bad'"
      :title="health.online ? 'Ollama 已连接' : 'Ollama 未连接'"
      @click="refreshHealth"
    ></button>
  </div>
</template>

<style scoped>
.sidebar-inner {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
}

.sidebar-inner.is-collapsed {
  align-items: center;
}

.sidebar-inner.is-collapsed .brand {
  flex-direction: column;
  gap: 8px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
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
  background: var(--card-bg);
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
  background: var(--surface-hover);
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

.session-item:hover .session-del,
.session-del:focus-visible {
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
  margin-left: 0;
}

.kb-entry + .kb-entry {
  margin-left: 0;
}

.health {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-subtle);
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
</style>
