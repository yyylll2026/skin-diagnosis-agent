import { computed, ref } from 'vue'
import {
  getHealth,
  getMe,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  setUnauthorizedHandler,
} from '../api'
import { ROLE_LABELS } from '../constants/roles'

/**
 * 登录状态。
 *
 * 与 `useSessions` 一样用**模块级 ref** 而不是 Pinia：全局只有一份登录态，
 * 没有复杂的派生与模块化需求，不值得引入状态管理库。
 *
 * ## 前端不持有任何凭据
 *
 * 会话在服务端，通过 httpOnly cookie 传递，JS 根本读不到。
 * 因此这里只存「我是谁、有什么权限」这类展示用信息——
 * **权限数组只用于隐藏入口，真正的拦截在服务端**。
 * 前端隐藏按钮是体验优化，不是安全手段。
 */

const user = ref(null)
const permissions = ref([])
const authEnabled = ref(false)
const allowRegistration = ref(false)
/** 是否已探测过一次登录态（避免启动瞬间闪烁登录框） */
const ready = ref(false)
const loginOpen = ref(false)

const isLoggedIn = computed(() => !!user.value)
const isAdmin = computed(() => user.value?.role === 'admin')
const displayName = computed(
  () => user.value?.display_name || user.value?.username || ''
)
/** 当前角色的中文标签，未知角色原样显示，避免新角色在侧栏出现空文案 */
const roleLabel = computed(
  () => (user.value ? ROLE_LABELS[user.value.role] || user.value.role : '')
)

/**
 * 判断是否拥有某权限点。
 *
 * 仅用于控制界面显隐。**不要用它做安全判断**——
 * 任何人都能在浏览器控制台改掉这里的返回值。
 */
function can(permission) {
  return permissions.value.includes(permission)
}

/** 打开登录框（由 401 拦截器或路由逻辑调用） */
function requireLogin() {
  loginOpen.value = true
}

/**
 * 启动时探测登录态。
 *
 * 顺序很重要：先问 `/system/health` 拿到「这个部署要不要登录」，
 * 再决定是否需要调 `/auth/me`。鉴权关闭时后端把每个请求都当作本地管理员，
 * 此时不该弹登录框。
 */
async function bootstrap() {
  try {
    const health = await getHealth()
    authEnabled.value = !!health.auth_enabled
    allowRegistration.value = !!health.allow_registration
  } catch (err) {
    // 后端不可达：保持「未启用鉴权」的假设，后续请求会自然报错
    authEnabled.value = false
    ready.value = true
    return
  }

  if (!authEnabled.value) {
    ready.value = true
    return
  }

  try {
    applyAuth(await getMe())
  } catch (err) {
    // 401 是正常路径（还没登录），不是错误
    clearAuth()
  } finally {
    ready.value = true
  }
}

function applyAuth(payload) {
  user.value = payload?.user || null
  permissions.value = Array.isArray(payload?.permissions) ? payload.permissions : []
}

function clearAuth() {
  user.value = null
  permissions.value = []
}

async function login(username, password) {
  applyAuth(await apiLogin(username, password))
  loginOpen.value = false
  return user.value
}

async function register(username, password, displayNameValue = '') {
  applyAuth(await apiRegister(username, password, displayNameValue))
  loginOpen.value = false
  return user.value
}

async function logout() {
  try {
    await apiLogout()
  } finally {
    // 即使后端调用失败也要清掉本地状态——否则界面会停在「看起来已登录」
    clearAuth()
  }
}

/** 注册 401 处理器（在 App 挂载时调用一次） */
function installUnauthorizedHandler() {
  setUnauthorizedHandler(() => {
    if (!authEnabled.value) return
    clearAuth()
    requireLogin()
  })
}

export function useAuth() {
  return {
    user,
    permissions,
    authEnabled,
    allowRegistration,
    ready,
    loginOpen,
    isLoggedIn,
    isAdmin,
    roleLabel,
    displayName,
    can,
    requireLogin,
    bootstrap,
    applyAuth,
    clearAuth,
    login,
    register,
    logout,
    installUnauthorizedHandler,
  }
}
