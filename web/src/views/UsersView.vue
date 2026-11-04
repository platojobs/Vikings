<script setup lang="ts">
/**
 * 账号与权限管理（仅管理员可见）：
 * - 新建账号：用户名 + 初始口令 + 角色
 * - 行内调整角色、启停账号
 * - 重置口令、删除账号（保护当前登录账号与最后一个管理员）
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createUser, deleteUser, fetchUsers, updateUser } from '@/api/users'
import { getStoredUser } from '@/utils/auth'
import { formatDateTime } from '@/utils/format'
import { userRoleLabel, userRoleTagType, type TagType } from '@/utils/labels'
import type { AppUser, UserRole } from '@/types'

const users = ref<AppUser[]>([])
const loading = ref(false)
const saving = ref(false)
const currentUsername = getStoredUser()?.username ?? ''

const ROLE_OPTIONS: Array<{ label: string; value: UserRole; desc: string }> = [
  { label: '管理员', value: 'admin', desc: '全部权限，含阈值配置与账号管理' },
  { label: '运维', value: 'operator', desc: '可下发指令、创建 OTA、确认告警' },
  { label: '只读', value: 'viewer', desc: '仅可查看设备、告警与审计日志' },
]

async function loadUsers(): Promise<void> {
  loading.value = true
  try {
    users.value = await fetchUsers()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    loading.value = false
  }
}

// ==================== 新建账号 ====================

const createVisible = ref(false)
const createForm = reactive<{ username: string; password: string; role: UserRole }>({
  username: '',
  password: '',
  role: 'viewer',
})

function openCreate(): void {
  createForm.username = ''
  createForm.password = ''
  createForm.role = 'viewer'
  createVisible.value = true
}

async function handleCreate(): Promise<void> {
  const username = createForm.username.trim()
  if (!/^[a-zA-Z0-9_.-]{2,32}$/.test(username)) {
    ElMessage.warning('用户名需为 2~32 位字母、数字或 _.-')
    return
  }
  if (createForm.password.length < 6) {
    ElMessage.warning('初始口令至少 6 位')
    return
  }

  saving.value = true
  try {
    const created = await createUser({
      username,
      password: createForm.password,
      role: createForm.role,
    })
    users.value = [...users.value, created]
    createVisible.value = false
    ElMessage.success(`账号 ${created.username} 已创建`)
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    saving.value = false
  }
}

// ==================== 行内调整角色 / 启停 ====================

const busyUser = ref('')

async function handleRoleChange(row: AppUser): Promise<void> {
  busyUser.value = row.username
  try {
    await updateUser(row.username, { role: row.role })
    ElMessage.success(`已将 ${row.username} 设为${userRoleLabel(row.role)}`)
  } catch {
    await loadUsers()
  } finally {
    busyUser.value = ''
  }
}

async function handleToggleActive(row: AppUser): Promise<void> {
  busyUser.value = row.username
  try {
    await updateUser(row.username, { active: row.active })
    ElMessage.success(row.active ? `${row.username} 已启用` : `${row.username} 已停用`)
  } catch {
    await loadUsers()
  } finally {
    busyUser.value = ''
  }
}

// ==================== 重置口令 ====================

const resetVisible = ref(false)
const resetTarget = ref('')
const resetPassword = ref('')

function openReset(row: AppUser): void {
  resetTarget.value = row.username
  resetPassword.value = ''
  resetVisible.value = true
}

async function handleReset(): Promise<void> {
  if (resetPassword.value.length < 6) {
    ElMessage.warning('新口令至少 6 位')
    return
  }
  saving.value = true
  try {
    await updateUser(resetTarget.value, { password: resetPassword.value })
    resetVisible.value = false
    ElMessage.success(`${resetTarget.value} 的口令已重置`)
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    saving.value = false
  }
}

// ==================== 删除 ====================

async function handleDelete(row: AppUser): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除账号 ${row.username}？该操作不可恢复。`,
      '删除账号',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteUser(row.username)
    users.value = users.value.filter((u) => u.username !== row.username)
    ElMessage.success('账号已删除')
  } catch {
    // 错误提示已由 HTTP 层统一处理
  }
}

const adminCount = computed(() => users.value.filter((u) => u.role === 'admin' && u.active).length)

function roleTagType(role: string): TagType {
  return userRoleTagType(role)
}

onMounted(loadUsers)
</script>

<template>
  <div class="page users-page">
    <el-alert type="info" :closable="false" class="role-hint">
      角色决定菜单与操作权限：管理员可管理账号与阈值，运维可下发指令 / OTA / 确认告警，只读仅查询。
      当前共有 {{ adminCount }} 个可用管理员，系统始终保留至少一个。
    </el-alert>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">后台账号（{{ users.length }}）</span>
          <div class="header-actions">
            <el-button size="small" :loading="loading" @click="loadUsers">刷新</el-button>
            <el-button size="small" type="primary" @click="openCreate">新建账号</el-button>
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="users" row-key="username">
        <el-table-column label="用户名" min-width="150">
          <template #default="{ row }">
            <span class="username">{{ row.username }}</span>
            <el-tag v-if="row.username === currentUsername" size="small" effect="plain" class="self-tag">
              当前登录
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="角色" width="150">
          <template #default="{ row }">
            <el-select
              v-model="row.role"
              size="small"
              :disabled="busyUser === row.username"
              class="role-select"
              @change="handleRoleChange(row)"
            >
              <el-option
                v-for="item in ROLE_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              >
                <div class="role-option">
                  <el-tag :type="roleTagType(item.value)" size="small" effect="plain">
                    {{ item.label }}
                  </el-tag>
                  <span class="role-desc">{{ item.desc }}</span>
                </div>
              </el-option>
            </el-select>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-switch
              v-model="row.active"
              :disabled="row.username === currentUsername || busyUser === row.username"
              active-text="启用"
              inactive-text="停用"
              @change="handleToggleActive(row)"
            />
          </template>
        </el-table-column>

        <el-table-column label="最近登录" min-width="170">
          <template #default="{ row }">
            {{ row.last_login_at ? formatDateTime(row.last_login_at) : '尚未登录' }}
          </template>
        </el-table-column>

        <el-table-column label="创建时间" min-width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openReset(row)">重置口令</el-button>
            <el-button
              link
              type="danger"
              :disabled="row.username === currentUsername"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无账号" :image-size="80" />
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="createVisible" title="新建账号" width="440px">
      <el-form label-position="top">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" placeholder="2~32 位字母、数字或 _.-" />
        </el-form-item>
        <el-form-item label="初始口令">
          <el-input v-model="createForm.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="createForm.role">
            <el-radio-button v-for="item in ROLE_OPTIONS" :key="item.value" :value="item.value">
              {{ item.label }}
            </el-radio-button>
          </el-radio-group>
          <div class="role-tip">
            {{ ROLE_OPTIONS.find((item) => item.value === createForm.role)?.desc }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetVisible" :title="`重置口令 · ${resetTarget}`" width="420px">
      <el-form label-position="top">
        <el-form-item label="新口令">
          <el-input v-model="resetPassword" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleReset">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.role-hint {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.username {
  font-weight: 500;
}

.self-tag {
  margin-left: 8px;
}

.role-select {
  width: 120px;
}

.role-option {
  display: flex;
  align-items: center;
  gap: 8px;
}

.role-desc {
  color: #94a3b8;
  font-size: 12px;
}

.role-tip {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
}
</style>
