<script setup lang="ts">
/**
 * 系统状态：网关 / Broker / 存储 / 后台任务的只读运维快照（仅管理员）。
 *
 * 定位是排障入口：后台其他页面回答「业务上发生了什么」，
 * 这一页回答「故障在哪一层」——网关进程？MQTT 连接？数据库容量？
 * 每 10 秒自动刷新一次，不订阅 MQTT，Broker 断开时本页仍可用。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchSystemStatus } from '@/api/system'
import { formatDateTime } from '@/utils/format'
import type { SystemStatus } from '@/types'

const status = ref<SystemStatus | null>(null)
const loading = ref(true)
const error = ref('')
let timer: ReturnType<typeof setInterval> | null = null

async function loadStatus(): Promise<void> {
  try {
    status.value = await fetchSystemStatus()
    error.value = ''
  } catch {
    error.value = '无法获取系统状态，请确认网关已启动且当前账号具备管理员权限'
  } finally {
    loading.value = false
  }
}

/** 运行时长拆成「天/时/分」，秒级细节对运维判断没有意义 */
function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days} 天 ${hours} 小时`
  if (hours > 0) return `${hours} 小时 ${minutes} 分`
  return `${minutes} 分钟`
}

const onlineRate = computed(() => {
  const storage = status.value?.storage
  if (!storage || storage.device_total === 0) return '—'
  return `${Math.round((storage.device_online / storage.device_total) * 100)}%`
})

const totalUptime = computed(() => formatUptime(status.value?.gateway.uptime_seconds ?? 0))

onMounted(() => {
  loadStatus()
  timer = setInterval(loadStatus, 10_000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div v-loading="loading" class="page system-page">
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
    />

    <el-alert
      v-if="status && !status.mqtt.connected"
      type="warning"
      show-icon
      :closable="false"
      title="MQTT Broker 未连接"
    >
      <template #default>
        设备上报与指令下发均不可用，最近错误：{{ status.mqtt.last_error || '—' }}
      </template>
    </el-alert>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">运行概览</span>
          <span class="section-desc">
            数据生成于 {{ formatDateTime(status.generated_at) }}，每 10 秒自动刷新
          </span>
        </div>
        <el-button size="small" @click="loadStatus">立即刷新</el-button>
      </div>

      <el-row :gutter="14" class="metric-row">
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">网关运行时长</div>
            <div class="metric-value">{{ totalUptime }}</div>
            <div class="metric-extra">
              版本 {{ status.gateway.version }} · 启动于
              {{ formatDateTime(status.gateway.started_at) }}
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">MQTT Broker</div>
            <div class="metric-value" :class="status.mqtt.connected ? 'ok' : 'bad'">
              {{ status.mqtt.connected ? '已连接' : '未连接' }}
            </div>
            <div class="metric-extra">
              {{ status.mqtt.host }}:{{ status.mqtt.port }} · 已消费
              {{ status.mqtt.messages_handled }} 条
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">设备在线率</div>
            <div class="metric-value">{{ onlineRate }}</div>
            <div class="metric-extra">
              在线 {{ status.storage.device_online }} / 总数
              {{ status.storage.device_total }}
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <div class="metric-card">
            <div class="metric-label">时序记录</div>
            <div class="metric-value">{{ status.storage.record_total }}</div>
            <div class="metric-extra">
              未确认告警 {{ status.storage.alert_open }} / 累计
              {{ status.storage.alert_total }}
            </div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <span class="section-title">存储与容量</span>
      </div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="数据库">
          {{ status.storage.backend === 'sqlite' ? 'SQLite（本地演示）' : 'PostgreSQL' }}
        </el-descriptions-item>
        <el-descriptions-item label="设备状态分布">
          在线 {{ status.storage.device_online }} · 离线
          {{ status.storage.device_offline }}
        </el-descriptions-item>
        <el-descriptions-item label="最早一条上报">
          {{ formatDateTime(status.storage.record_oldest_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="最新一条上报">
          {{ formatDateTime(status.storage.record_newest_at) }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card v-if="status" shadow="never">
      <div class="section-head">
        <span class="section-title">后台任务与业务参数</span>
        <span class="section-desc">只读展示，修改需调整网关环境变量后重启</span>
      </div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="离线判定阈值">
          {{ status.scheduler.offline_timeout_seconds }} 秒未上报即置离线
        </el-descriptions-item>
        <el-descriptions-item label="离线巡检周期">
          每 {{ status.scheduler.offline_sweep_interval_seconds }} 秒执行一次
        </el-descriptions-item>
        <el-descriptions-item label="时序数据保留">
          {{ status.scheduler.data_retention_days }} 天
        </el-descriptions-item>
        <el-descriptions-item label="数据清理周期">
          每 {{ status.scheduler.data_purge_interval_seconds }} 秒执行一次
        </el-descriptions-item>
        <el-descriptions-item label="告警抑制窗口">
          同设备同指标 {{ status.scheduler.alert_suppress_seconds }} 秒内只报一次
        </el-descriptions-item>
        <el-descriptions-item label="告警外推 Webhook">
          <el-tag
            :type="status.notify.webhook_enabled ? 'success' : 'info'"
            size="small"
            effect="plain"
          >
            {{ status.notify.webhook_enabled ? '已启用' : '未启用' }}
          </el-tag>
          <span v-if="status.notify.webhook_enabled" class="webhook-target">
            {{ status.notify.webhook_target }}
          </span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-empty v-if="!loading && !status" description="暂无系统状态数据" :image-size="90" />
  </div>
</template>

<style scoped>
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.section-desc {
  margin-left: 10px;
  font-size: 12px;
  color: #94a3b8;
}

.metric-row {
  row-gap: 12px;
}

.metric-card {
  padding: 14px 16px;
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
}

.metric-label {
  font-size: 13px;
  color: #64748b;
}

.metric-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.1;
  color: #0f172a;
}

.metric-value.ok {
  color: #16a34a;
}

.metric-value.bad {
  color: #dc2626;
}

.metric-extra {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
}

.webhook-target {
  margin-left: 8px;
  font-size: 12px;
  color: #64748b;
}
</style>
