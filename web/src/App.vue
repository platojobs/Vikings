<script setup lang="ts">
/** 应用外壳：登录页不显示导航，其余页面共用顶部导航与 MQTT 状态 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { clearAuth, getStoredUser } from '@/utils/auth'
import { onStatusChange } from '@/services/mqtt'

const route = useRoute()
const router = useRouter()

const isLogin = computed(() => route.name === 'login')
const activeMenu = computed(() => route.path)
const username = computed(() => getStoredUser()?.username ?? 'admin')

const mqttConnected = ref(false)
let offStatus: (() => void) | null = null

onMounted(() => {
  offStatus = onStatusChange((connected) => {
    mqttConnected.value = connected
  })
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
          <el-menu-item index="/devices">设备管理</el-menu-item>
          <el-menu-item index="/history">历史查询</el-menu-item>
          <el-menu-item index="/ota">OTA 管理</el-menu-item>
        </el-menu>

        <div class="header-right">
          <el-tag :type="mqttConnected ? 'success' : 'info'" effect="dark" size="small">
            {{ mqttConnected ? 'MQTT 已连接' : 'MQTT 连接中' }}
          </el-tag>
          <span class="username">{{ username }}</span>
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
