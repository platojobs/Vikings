<script setup lang="ts">
/** 登录页：调用 /api/auth/login，token 写入 localStorage，成功后跳回原页面 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '@/api/auth'
import { setAuth } from '@/utils/auth'

const router = useRouter()
const route = useRoute()

const form = reactive({ username: 'admin', password: 'admin123' })
const loading = ref(false)

/** 三种角色的演示账号，点击即可填充，便于直接演示权限差异 */
const demoAccounts = [
  { username: 'admin', password: 'admin123', label: '管理员' },
  { username: 'operator', password: 'operator123', label: '运维' },
  { username: 'viewer', password: 'viewer123', label: '只读' },
]

function fillDemo(account: (typeof demoAccounts)[number]): void {
  form.username = account.username
  form.password = account.password
}

async function handleSubmit(): Promise<void> {
  if (!form.username.trim() || !form.password.trim()) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const data = await login({
      username: form.username.trim(),
      password: form.password,
    })
    setAuth(data.token, data.user, data.refresh_token)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch {
    // 错误提示已在 HTTP 封装层统一弹出，这里只需结束 loading
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-intro">
      <div class="login-logo">IoT</div>
      <h1>智能硬件全链路管理平台</h1>
      <p class="login-subtitle">设备 · 网关 · 云端一体化运维控制台</p>
      <ul class="login-features">
        <li>MQTT 实时遥测，毫秒级看板刷新</li>
        <li>AI 异常诊断，告警文案自动生成</li>
        <li>OTA 批量升级，逐台设备进度可视</li>
      </ul>
    </div>

    <el-card class="login-card" shadow="always">
      <h2 class="login-title">后台登录</h2>
      <p class="login-tip">请输入运维账号以访问管理控制台</p>

      <el-form label-position="top" @submit.prevent="handleSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            size="large"
            placeholder="请输入用户名"
            autocomplete="username"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button
          class="login-button"
          type="primary"
          size="large"
          :loading="loading"
          native-type="submit"
        >
          登录
        </el-button>
      </el-form>

      <div class="demo-accounts">
        <span class="demo-label">演示账号（点击填充）</span>
        <div class="demo-list">
          <el-tag
            v-for="account in demoAccounts"
            :key="account.username"
            class="demo-tag"
            effect="plain"
            @click="fillDemo(account)"
          >
            {{ account.label }} · {{ account.username }}
          </el-tag>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 80px;
  min-height: 100vh;
  padding: 40px;
  background: radial-gradient(circle at 20% 20%, #1e3a8a 0%, #0f172a 45%, #082f49 100%);
}

.login-intro {
  max-width: 460px;
  color: #e2e8f0;
}

.login-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: linear-gradient(135deg, #38bdf8, #2563eb);
  color: #ffffff;
  font-weight: 700;
  letter-spacing: 1px;
}

.login-intro h1 {
  margin: 20px 0 10px;
  font-size: 34px;
  line-height: 1.3;
  color: #ffffff;
}

.login-subtitle {
  margin: 0;
  font-size: 15px;
  color: #94a3b8;
}

.login-features {
  margin: 28px 0 0;
  padding: 0;
  list-style: none;
}

.login-features li {
  position: relative;
  margin-bottom: 12px;
  padding-left: 18px;
  font-size: 14px;
  color: #cbd5e1;
}

.login-features li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #38bdf8;
}

.login-card {
  width: 380px;
  border: none;
  border-radius: 14px;
  box-shadow: 0 24px 60px rgba(2, 6, 23, 0.45);
}

.login-title {
  margin: 4px 0 6px;
  font-size: 20px;
  color: #0f172a;
}

.login-tip {
  margin: 0 0 22px;
  font-size: 13px;
  color: #94a3b8;
}

.login-button {
  width: 100%;
  margin-top: 6px;
}

.demo-accounts {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px dashed #e2e8f0;
  text-align: center;
}

.demo-label {
  font-size: 12px;
  color: #94a3b8;
}

.demo-list {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 8px;
}

.demo-tag {
  cursor: pointer;
}

@media (max-width: 900px) {
  .login-page {
    gap: 32px;
    flex-direction: column;
  }

  .login-intro {
    text-align: center;
  }

  .login-features {
    display: none;
  }

  .login-intro h1 {
    font-size: 26px;
  }

  .login-card {
    width: min(380px, 100%);
  }
}
</style>
