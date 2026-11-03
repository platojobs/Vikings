<script setup lang="ts">
/** 设备在线状态徽标：在线绿 / 离线灰 */
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const online = computed(() => props.status === 'online')
</script>

<template>
  <span class="status-tag" :class="online ? 'is-online' : 'is-offline'">
    <i class="status-dot"></i>
    {{ online ? '在线' : '离线' }}
  </span>
</template>

<style scoped>
.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #64748b;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #94a3b8;
}

.is-online {
  color: #15803d;
}

.is-online .status-dot {
  background: #22c55e;
  box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.18);
}

.is-offline .status-dot {
  background: #94a3b8;
}
</style>
