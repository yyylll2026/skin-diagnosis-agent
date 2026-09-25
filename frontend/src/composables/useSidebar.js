import { ref } from 'vue'

/**
 * 侧栏布局状态（模块级单例）。
 *
 * 两种形态：
 * - 桌面端（> 860px）：侧栏常驻，`collapsed` 控制展开 / 收起为窄条；
 * - 移动端（≤ 860px）：侧栏变为浮层抽屉，`mobileOpen` 控制显隐，带遮罩。
 *
 * App（渲染侧栏与遮罩）和 ChatPanel（渲染汉堡按钮）都消费这份状态，
 * 用 composable 共享避免 props / emits 层层传递。
 */
const MOBILE_QUERY = '(max-width: 860px)'

const isMobile = ref(false)
const collapsed = ref(false)
const mobileOpen = ref(false)

let mediaQueryList = null

function onMediaChange(event) {
  isMobile.value = event.matches
  if (!event.matches) mobileOpen.value = false
}

/** 在应用启动时调用一次，绑定断点监听 */
function initSidebar() {
  if (mediaQueryList || typeof window === 'undefined' || !window.matchMedia) return
  mediaQueryList = window.matchMedia(MOBILE_QUERY)
  isMobile.value = mediaQueryList.matches
  mediaQueryList.addEventListener('change', onMediaChange)
}

function toggleCollapsed() {
  collapsed.value = !collapsed.value
}

function openMobile() {
  if (isMobile.value) mobileOpen.value = true
}

function closeMobile() {
  mobileOpen.value = false
}

export function useSidebar() {
  return {
    isMobile,
    collapsed,
    mobileOpen,
    initSidebar,
    toggleCollapsed,
    openMobile,
    closeMobile,
  }
}
