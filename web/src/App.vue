<script setup lang="ts">
/** 应用外壳：登录页不显示导航，其余页面共用顶部导航与 MQTT 状态 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { fetchMe, logout as logoutApi } from '@/api/auth'
import { clearAuth, getRefreshToken, getStoredUser, getToken, setAuth } from '@/utils/auth'
import { onStatusChange } from '@/services/mqtt'
import { setRole } from '@/utils/permissions'
import { userRoleLabel, userRoleTagType } from '@/utils/labels'

const route = useRoute()
const router = useRouter()

const isLogin = computed(() => route.name === 'login')
const activeMenu = computed(() => route.path)
const username = computed(() => getStoredUser()?.username ?? 'admin')

/** 角色随 /api/auth/me 刷新，用于菜单显示（仅管理员可见账号管理） */
const role = ref(getStoredUser()?.role ?? '')
const isAdmin = computed(() => role.value === 'admin')
/** v1.31.0：投递历史对值班 operator 与 admin 开放 */
const canViewNotifications = computed(() => role.value === 'admin' || role.value === 'operator')

const mqttConnected = ref(false)
let offStatus: (() => void) | null = null

onMounted(async () => {
  offStatus = onStatusChange((connected) => {
    mqttConnected.value = connected
  })

  if (!getToken()) return
  try {
    const me = await fetchMe()
    role.value = me.role
    setRole(me.role as 'admin' | 'operator' | 'viewer')
    setAuth(getToken(), { username: me.username, role: me.role })
    // 非管理员停在账号管理页时（例如权限被降级）回到仪表盘
    if (me.role !== 'admin' && route.path === '/users') {
      await router.replace('/dashboard')
    }
  } catch {
    // 401 已由 HTTP 层清除登录态，其余错误保持静默
  }
})

onBeforeUnmount(() => offStatus?.())

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确认退出登录？', '提示', {
      type: 'warning',
      confirmButtonText: '退出',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  // 先尽力吊销服务端 refresh（网络异常不阻塞本地退出），再清本地 access/refresh
  const refreshToken = getRefreshToken()
  if (refreshToken) {
    try {
      await logoutApi(refreshToken)
    } catch {
      // 登出接口失败（离线/已失效）时仍继续本地清理
    }
  }
  clearAuth()
  await router.replace('/login')
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="isLogin" />

    <el-container v-else class="app-layout">
      <el-header class="app-header">
        <div class="brand">
          <span class="brand-logo">IoT</span>
          <span class="brand-title">智能硬件全链路管理平台</span>
        </div>

        <el-menu
          class="app-menu"
          mode="horizontal"
          :default-active="activeMenu"
          :ellipsis="false"
          router
        >
          <el-menu-item index="/dashboard">实时仪表盘</el-menu-item>
          <el-menu-item index="/battery-health">电池续航</el-menu-item>
          <el-menu-item index="/devices">设备管理</el-menu-item>
          <el-menu-item index="/history">历史查询</el-menu-item>
          <el-menu-item index="/alerts">告警中心</el-menu-item>
          <el-menu-item index="/audit">审计日志</el-menu-item>
          <el-menu-item v-if="isAdmin" index="/users">账号管理</el-menu-item>
          <el-menu-item index="/ota">OTA 管理</el-menu-item>
          <el-menu-item v-if="canViewNotifications" index="/notifications">通知投递</el-menu-item>
          <el-menu-item v-if="isAdmin" index="/system">系统状态</el-menu-item>
        </el-menu>

        <div class="header-right">
          <el-tag :type="mqttConnected ? 'success' : 'info'" effect="dark" size="small">
            {{ mqttConnected ? 'MQTT 已连接' : 'MQTT 连接中' }}
          </el-tag>
          <span class="username">{{ username }}</span>
          <el-tag :type="userRoleTagType(role)" size="small" effect="plain">
            {{ userRoleLabel(role) }}
          </el-tag>
          <el-button link class="logout-btn" @click="handleLogout">退出登录</el-button>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-config-provider>
</template>

<style scoped>
.app-layout {
  min-height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  height: 64px;
  padding: 0 20px;
  background: #0f172a;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.brand-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border-radius: 9px;
  background: linear-gradient(135deg, #38bdf8, #2563eb);
  color: #ffffff;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.brand-title {
  color: #e2e8f0;
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  height: 64px;
  border-bottom: none;
  background: transparent;
}

.app-menu :deep(.el-menu-item) {
  height: 64px;
  color: #cbd5e1;
  border-bottom: none;
}

.app-menu :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.08);
}

.app-menu :deep(.el-menu-item.is-active) {
  color: #ffffff;
  background: rgba(56, 189, 248, 0.14);
  border-bottom: 2px solid #38bdf8;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  color: #cbd5e1;
  font-size: 13px;
}

.logout-btn {
  color: #cbd5e1;
}

.logout-btn:hover {
  color: #38bdf8;
}

.app-main {
  padding: 20px;
  background: #f5f7fa;
}
</style>
