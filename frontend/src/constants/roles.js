/**
 * 角色展示常量。
 *
 * 与后端 `app/schemas/auth.py` 的 `Role` 枚举一一对应。角色仅决定权限点集合
 * （真正的权限表在 `app/services/auth_service.py` 的 PERMISSIONS），
 * 前端只用它做展示文案——判断「能不能看到某入口」应优先用 `can(权限点)`，
 * 而不是拿 role 字符串比较，否则每加一个角色前端就得改一轮 if。
 */

/** 角色 → 中文标签 */
export const ROLE_LABELS = {
  admin: '管理员',
  expert: '专家',
  doctor: '医生',
  customer: '客户',
}

/** 角色 → el-tag 的 type（区分度从高到低） */
export const ROLE_TAG_TYPES = {
  admin: 'warning',
  expert: 'danger',
  doctor: 'success',
  customer: 'info',
}

/** 创建用户时的角色选项，顺序即权限从低到高 */
export const ROLE_OPTIONS = [
  { value: 'customer', label: '客户（提问 + 只读知识库 + 自己的记录）' },
  { value: 'doctor', label: '医生（问诊 + 读写全部患者记录）' },
  { value: 'expert', label: '专家（医生权限 + 知识库审核 + 诊断复核）' },
  { value: 'admin', label: '管理员（全部权限 + 用户管理）' },
]
