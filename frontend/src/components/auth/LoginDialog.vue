<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useAuth } from '../../composables/useAuth'

/**
 * 登录 / 注册对话框。
 *
 * 用对话框而不是独立路由页：项目目前没有 vue-router，
 * 引入它只为一个登录页不划算，而且全屏遮罩能天然挡住未登录时的界面操作。
 *
 * 未登录时**不显示关闭按钮**——关掉它只会看到一个空壳界面，
 * 不如引导用户完成登录（或注册）。
 */
const { loginOpen, allowRegistration, isLoggedIn, login, register } = useAuth()

const mode = ref('login')
const loading = ref(false)
const errorText = ref('')

const form = reactive({
  username: '',
  password: '',
  confirm: '',
  displayName: '',
})

const title = computed(() => (mode.value === 'login' ? '登录' : '注册'))
const submitText = computed(() => (mode.value === 'login' ? '登录' : '注册并登录'))

/** 前端预校验：只为了少一次往返，真正的规则在服务端 */
const localError = computed(() => {
  if (form.username.trim().length < 3) return '用户名至少 3 位'
  if (form.password.length < 8) return '密码至少 8 位'
  if (!/[A-Za-z]/.test(form.password) || !/\d/.test(form.password)) {
    return '密码需同时包含字母和数字'
  }
  if (mode.value === 'register' && form.password !== form.confirm) {
    return '两次输入的密码不一致'
  }
  return ''
})

const canSubmit = computed(() => !localError.value && !loading.value)

function resetForm() {
  form.username = ''
  form.password = ''
  form.confirm = ''
  form.displayName = ''
  errorText.value = ''
}

function switchMode(next) {
  mode.value = next
  errorText.value = ''
}

async function submit() {
  if (!canSubmit.value) return
  loading.value = true
  errorText.value = ''
  try {
    if (mode.value === 'login') {
      await login(form.username.trim(), form.password)
    } else {
      await register(form.username.trim(), form.password, form.displayName.trim())
    }
    resetForm()
  } catch (err) {
    errorText.value = err?.message || '操作失败，请重试'
  } finally {
    loading.value = false
  }
}

// 打开时清空上一次的输入与报错，避免「上次的密码错误提示」残留
watch(loginOpen, (open) => {
  if (open) resetForm()
})
</script>

<template>
  <el-dialog
    v-model="loginOpen"
    :title="title"
    width="min(380px, 92vw)"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="isLoggedIn"
    :show-close="isLoggedIn"
    class="login-dialog"
  >
    <p class="hint">皮肤诊断智能体 · 仅用于健康科普与初步参考</p>

    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名">
        <el-input
          v-model="form.username"
          placeholder="3 位以上，字母 / 数字 / 下划线"
          autocomplete="username"
          @keyup.enter="submit"
        />
      </el-form-item>

      <el-form-item label="密码">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          placeholder="8 位以上，含字母和数字"
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
          @keyup.enter="submit"
        />
      </el-form-item>

      <template v-if="mode === 'register'">
        <el-form-item label="确认密码">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            placeholder="再输入一次"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="显示名（可选）">
          <el-input v-model="form.displayName" placeholder="留空则用用户名" />
        </el-form-item>
      </template>

      <p v-if="errorText" class="error">{{ errorText }}</p>
      <p v-else-if="localError && form.username" class="tip">{{ localError }}</p>
    </el-form>

    <template #footer>
      <div class="footer">
        <el-button type="primary" :loading="loading" :disabled="!canSubmit" @click="submit">
          {{ submitText }}
        </el-button>
        <div v-if="allowRegistration" class="switch">
          <span v-if="mode === 'login'">还没有账号？</span>
          <span v-else>已有账号？</span>
          <button type="button" class="link" @click="switchMode(mode === 'login' ? 'register' : 'login')">
            {{ mode === 'login' ? '注册一个' : '去登录' }}
          </button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 14px;
}

.error {
  font-size: 12.5px;
  color: var(--danger);
  line-height: 1.5;
}

.tip {
  font-size: 12.5px;
  color: var(--text-muted);
  line-height: 1.5;
}

.footer {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
}

.switch {
  font-size: 12.5px;
  color: var(--text-secondary);
  text-align: center;
}

.link {
  border: none;
  background: transparent;
  color: var(--brand);
  cursor: pointer;
  font-size: 12.5px;
  padding: 0 2px;
}

.link:hover {
  text-decoration: underline;
}
</style>
