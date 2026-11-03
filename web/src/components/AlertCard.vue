<script setup lang="ts">
/** 实时告警卡片：critical 红色 / warning 橙色 */
import { formatDateTime, formatMetric } from '@/utils/format'
import type { AlertView } from '@/types'

defineProps<{ alert: AlertView }>()
</script>

<template>
  <div class="alert-card" :class="alert.level">
    <div class="alert-head">
      <el-tag :type="alert.level === 'critical' ? 'danger' : 'warning'" effect="dark" size="small">
        {{ alert.level === 'critical' ? '严重' : '警告' }}
      </el-tag>
      <span class="device">{{ alert.deviceName || alert.deviceId }}</span>
      <span class="time">{{ formatDateTime(alert.ts) }}</span>
    </div>
    <p class="message">{{ alert.message }}</p>
    <div class="meta">
      <span class="metric">
        {{ alert.metric }}：{{ formatMetric(alert.value, 2) }}（阈值 {{ formatMetric(alert.threshold, 2) }}）
      </span>
      <el-tag v-if="alert.aiGenerated" type="info" effect="plain" size="small">AI 生成</el-tag>
    </div>
  </div>
</template>

<style scoped>
.alert-card {
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  border-left-width: 4px;
  background: #ffffff;
}

.alert-card.critical {
  border-left-color: #ef4444;
  background: #fef2f2;
  border-color: #fecaca;
}

.alert-card.warning {
  border-left-color: #f59e0b;
  background: #fff7ed;
  border-color: #fed7aa;
}

.alert-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.device {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
}

.time {
  margin-left: auto;
  font-size: 12px;
  color: #94a3b8;
  white-space: nowrap;
}

.message {
  margin: 8px 0 6px;
  font-size: 13px;
  line-height: 1.6;
  color: #334155;
}

.meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.metric {
  font-size: 12px;
  color: #64748b;
}
</style>
