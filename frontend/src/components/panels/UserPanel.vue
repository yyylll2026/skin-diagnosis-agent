<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createUser,
  deleteUser,
  forceLogoutUser,
  listAuditLogs,
  listUsers,
  resetUserPassword,
  updateUser,
} from '../../api'
import { useAuth } from '../../composables/useAuth'
import { useSidebar } from '../../composables/useSidebar'
import { ROLE_LABELS, ROLE_OPTIONS, ROLE_TAG_TYPES } from '../../constants/roles'

/**
 * 用户管理抽屉（仅管理员可见）。
 *
 * 前端拦截只做体验：所有操作在服务端都要求 `user:write` 权限点，
 * 客户即使手工构造请求也会拿到 403。
 *
 * 有两条约束前端不做校验，因为**服务端才是权威**：
 * 「不能删/禁用自己」「必须保留至少一个管理员」。
 * 前端只需要把服务端返回的 403 原因展示出来即可——
 * 否则规则要写两遍，迟早不一致。
 */
const visible = defineModel({ type: Boolean, default: false })

const { user: currentUser } = useAuth()
const { isMobile } = useSidebar()

/** 窄屏下抽屉占满全宽 */
const drawerSize = computed(() => (isMobile.value ? '100%' : '720px'))

const tab = ref('users')
const loading = ref(false)
const keyword = ref('')
const users = ref([])
const logs = ref([])

const createOpen = ref(false)
const creating = ref(false)
const createForm = reactive({
  username: '',
  password: '',
  display_name: '',
  role: 'customer',
})

const resetOpen = ref(false)
const resetting = ref(false)
const resetTarget = ref(null)
const resetForm = reactive({ password: '' })

async function loadUsers() {
  loading.value = true
  try {
    users.value = await listUsers(keyword.value)
  } catch (err) {
    ElMessage.error(err?.message || '加载用户列表失败')
  } finally {
    loading.value = false
  }
}

async function loadLogs() {
  loading.value = true
  try {
    logs.value = await listAuditLogs(100)
  } catch (err) {
    ElMessage.error(err?.message || '加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function refresh() {
  if (tab.value === 'users') loadUsers()
  else loadLogs()
}

// immediate：面板由父级懒加载（首次打开才挂载），挂载时即需拉一次数据
watch(
  [visible, tab],
  () => {
    if (visible.value) refresh()
  },
  { immediate: true }
)

/** 统一的「执行 + 报错 + 刷新」包装，省掉每个操作重复一遍 try/catch */
async function run(action, successText) {
  try {
    await action()
    if (successText) ElMessage.success(successText)
    await loadUsers()
  } catch (err) {
    ElMessage.error(err?.message || '操作失败')
  }
}

async function submitCreate() {
  if (!createForm.username || !createForm.password) {
    ElMessage.warning('请填写用户名与密码')
    return
  }
  creating.value = true
  try {
    await createUser({ ...createForm })
    ElMessage.success(`已创建 ${createForm.username}`)
    createOpen.value = false
    createForm.username = ''
    createForm.password = ''
    createForm.display_name = ''
    createForm.role = 'customer'
    await loadUsers()
  } catch (err) {
    ElMessage.error(err?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

function toggleStatus(row) {
  const next = row.status === 'active' ? 'disabled' : 'active'
  const verb = next === 'disabled' ? '禁用' : '启用'
  ElMessageBox.confirm(
    next === 'disabled'
      ? `禁用 ${row.username} 会立即踢掉其所有登录会话，确定吗？`
      : `确定启用 ${row.username} 吗？`,
    `${verb}账号`,
    { type: 'warning' }
  )
    .then(() => run(() => updateUser(row.user_id, { status: next }), `${row.username} 已${verb}`))
    .catch(() => {})
}

/**
 * 改角色：四种角色没法用二元开关表达，改为在下拉里选定目标角色。
 * 拒绝原因（比如「不能降级最后一个管理员」）以服务端返回为准，原样展示。
 */
function changeRole(row, role) {
  if (!role || role === row.role) return
  run(
    () => updateUser(row.user_id, { role }),
    `${row.username} 已设为 ${ROLE_LABELS[role] || role}`
  )
}

function openReset(row) {
  resetTarget.value = row
  resetForm.password = ''
  resetOpen.value = true
}

async function submitReset() {
  if (resetForm.password.length < 8) {
    ElMessage.warning('新密码至少 8 位')
    return
  }
  resetting.value = true
  try {
    await resetUserPassword(resetTarget.value.user_id, resetForm.password)
    ElMessage.success('密码已重置，该用户所有会话已失效')
    resetOpen.value = false
  } catch (err) {
    ElMessage.error(err?.message || '重置失败')
  } finally {
    resetting.value = false
  }
}

function kick(row) {
  run(() => forceLogoutUser(row.user_id), `${row.username} 已被强制下线`)
}

function remove(row) {
  ElMessageBox.confirm(
    `删除 ${row.username}？该操作不可恢复（其对话历史会保留，用于审计）。`,
    '删除用户',
    { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' }
  )
    .then(() => run(() => deleteUser(row.user_id), `已删除 ${row.username}`))
    .catch(() => {})
}

function isSelf(row) {
  return currentUser.value?.user_id === row.user_id
}
</script>

<template>
  <el-drawer v-model="visible" title="用户与审计" :size="drawerSize" direction="rtl">
    <el-tabs v-model="tab">
      <el-tab-pane label="用户管理" name="users">
        <div class="toolbar">
          <el-input
            v-model="keyword"
            class="search"
            placeholder="搜索用户名或显示名"
            clearable
            @keyup.enter="loadUsers"
            @clear="loadUsers"
          />
          <el-button @click="loadUsers">搜索</el-button>
          <el-button type="primary" @click="createOpen = true">新建用户</el-button>
        </div>

        <el-table v-loading="loading" :data="users" size="small" class="table">
          <el-table-column prop="username" label="用户名" min-width="120">
            <template #default="{ row }">
              {{ row.username }}
              <el-tag v-if="isSelf(row)" size="small" type="info" effect="plain">我</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="display_name" label="显示名" min-width="100" />
          <el-table-column label="角色" width="200">
            <template #default="{ row }">
              <el-tag
                :type="ROLE_TAG_TYPES[row.role] || 'info'"
                size="small"
                effect="plain"
                class="role-tag"
              >
                {{ ROLE_LABELS[row.role] || row.role }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small" effect="plain">
                {{ row.status === 'active' ? '启用' : '禁用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="last_login_at" label="最后登录" min-width="140" />
          <el-table-column label="操作" width="340" fixed="right">
            <template #default="{ row }">
              <el-select
                :model-value="row.role"
                size="small"
                class="role-select"
                @change="(role) => changeRole(row, role)"
              >
                <el-option
                  v-for="option in ROLE_OPTIONS"
                  :key="option.value"
                  :label="option.value === row.role ? `${option.label}（当前）` : option.label"
                  :value="option.value"
                  :disabled="option.value === row.role"
                />
              </el-select>
              <el-button link size="small" @click="toggleStatus(row)">
                {{ row.status === 'active' ? '禁用' : '启用' }}
              </el-button>
              <el-button link size="small" @click="openReset(row)">重置密码</el-button>
              <el-button link size="small" @click="kick(row)">踢下线</el-button>
              <el-button link size="small" type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <p class="footnote">
          系统无法删除或降级最后一个启用中的管理员；管理员也不能禁用或删除自己。
          这些约束在服务端强制，前端只是把拒绝原因原样展示。
        </p>
      </el-tab-pane>

      <el-tab-pane label="审计日志" name="audit">
        <div class="toolbar">
          <el-button @click="loadLogs">刷新</el-button>
        </div>
        <el-table v-loading="loading" :data="logs" size="small" class="table">
          <el-table-column prop="created_at" label="时间" width="150" />
          <el-table-column prop="action" label="动作" width="150" />
          <el-table-column prop="actor" label="操作者" width="100" />
          <el-table-column prop="target" label="对象" min-width="140" show-overflow-tooltip />
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag :type="row.result === 'ok' ? 'success' : 'warning'" size="small" effect="plain">
                {{ row.result }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="client_ip" label="IP" width="120" />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="createOpen" title="新建用户" width="380px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" placeholder="3 位以上，字母 / 数字 / 下划线" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="createForm.password" type="password" show-password placeholder="8 位以上，含字母和数字" />
        </el-form-item>
        <el-form-item label="显示名（可选）">
          <el-input v-model="createForm.display_name" placeholder="留空则用用户名" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createForm.role" class="full">
            <el-option
              v-for="option in ROLE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetOpen" title="重置密码" width="360px" append-to-body>
      <p class="dialog-hint">
        将重置 <strong>{{ resetTarget?.username }}</strong> 的密码，
        该用户所有登录会话会立即失效。
      </p>
      <el-input v-model="resetForm.password" type="password" show-password placeholder="新密码，8 位以上含字母和数字" />
      <template #footer>
        <el-button @click="resetOpen = false">取消</el-button>
        <el-button type="primary" :loading="resetting" @click="submitReset">确认重置</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.search {
  max-width: 260px;
  flex: 1;
  min-width: 160px;
}

.table {
  width: 100%;
}

.footnote {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
}

.dialog-hint {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-bottom: 12px;
}

.full {
  width: 100%;
}

.role-select {
  width: 140px;
  margin-right: 10px;
}

.role-tag {
  margin-right: 6px;
}
</style>
