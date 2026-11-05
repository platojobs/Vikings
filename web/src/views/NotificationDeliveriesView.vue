<script setup lang="ts">
/**
 * 通知投递历史（v1.31.0，独立页面，operator + admin）。
 *
 * 定位：值班交接时回答「某条告警到底发没发、发到谁、为什么失败」。
 * 筛选与分页全部交给服务端（同一套参数语义），前端不做二次过滤；
 * 页面不参与通知配置，配置留在「系统状态」。
 */
import { onMounted, reactive, ref } from 'vue'
import { fetchNotificationDeliveries } from '@/api/notifications'
import { formatDateTime } from '@/utils/format'
import type {
  NotificationDelivery,
  NotificationDeliveryChannel,
  NotificationDeliveryStatus,
} from '@/types'

const loading = ref(false)
const items = ref<NotificationDelivery[]>([])
const total = ref(0)
const error = ref('')

const filters = reactive({
  page: 1,
  pageSize: 20,
  status: '' as NotificationDeliveryStatus | '',
  channel: '' as NotificationDeliveryChannel | '',
  deviceId: '',
  range: [] as string[],
})

const statusOptions = [
  { label: '待投递 / 重试中', value: 'pending' },
  { label: '成功', value: 'sent' },
  { label: '最终失败', value: 'failed' },
  { label: '被抑制', value: 'suppressed' },
]

const channelOptions = [
  { label: 'Webhook', value: 'webhook' },
  { label: 'SMTP', value: 'smtp' },
  { label: '静默抑制', value: 'mute' },
  { label: '规则未命中', value: 'rule' },
]

const channelLabels: Record<string, string> = {
  webhook: 'Webhook',
  smtp: 'SMTP',
  mute: '静默抑制',
  rule: '规则未命中',
}

const statusLabels: Record<string, string> = {
  pending: '待投递',
  sent: '成功',
  failed: '失败',
  suppressed: '被抑制',
}

function statusTagType(status: string): 'success' | 'danger' | 'info' | 'warning' {
  if (status === 'sent') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'pending') return 'warning'
  return 'info'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const page = await fetchNotificationDeliveries({
      page: filters.page,
      page_size: filters.pageSize,
      status: filters.status,
      channel: filters.channel,
      device_id: filters.deviceId.trim(),
      // 选择器给出的是本地时间字符串；转成带时区的 ISO，服务端按 UTC 比较
      start: filters.range?.[0] ? new Date(filters.range[0]).toISOString() : undefined,
      end: filters.range?.[1] ? new Date(filters.range[1]).toISOString() : undefined,
    })
    items.value = page.items
    total.value = page.total
  } catch {
    items.value = []
    total.value = 0
    error.value = '投递历史加载失败，请确认网关可用且当前账号为 admin / operator'
  } finally {
    loading.value = false
  }
}

/** 筛选条件变化时回到第一页，避免停在越界页码出现空白 */
function applyFilters(): void {
  filters.page = 1
  load()
}

function resetFilters(): void {
  filters.status = ''
  filters.channel = ''
  filters.deviceId = ''
  filters.range = []
  applyFilters()
}

function handlePageChange(page: number): void {
  filters.page = page
  load()
}

function handleSizeChange(size: number): void {
  filters.pageSize = size
  filters.page = 1
  load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <el-card v-loading="loading" shadow="never">
      <div class="section-head">
        <div>
          <span class="section-title">通知投递历史</span>
          <span class="section-desc">
            每条记录对应一次告警的一个投递通道；尝试次数、累计耗时与最后错误均来自服务端
          </span>
        </div>
        <el-button size="small" @click="load">刷新</el-button>
      </div>

      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="filter-error" />

      <div class="filters">
        <el-date-picker
          v-model="filters.range"
          type="datetimerange"
          size="small"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          value-format="YYYY-MM-DDTHH:mm:ss"
          :clearable="true"
        />
        <el-select v-model="filters.status" size="small" placeholder="全部状态" clearable style="width: 150px" @change="applyFilters">
          <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-select v-model="filters.channel" size="small" placeholder="全部通道" clearable style="width: 140px" @change="applyFilters">
          <el-option v-for="item in channelOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-input
          v-model="filters.deviceId"
          size="small"
          placeholder="设备 ID（精确匹配）"
          clearable
          style="width: 200px"
          @keyup.enter="applyFilters"
          @clear="applyFilters"
        />
        <el-button size="small" type="primary" @click="applyFilters">查询</el-button>
        <el-button size="small" @click="resetFilters">重置</el-button>
      </div>

      <el-table :data="items" size="small" empty-text="暂无投递记录">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="device_id" label="设备" min-width="120" />
        <el-table-column prop="group_name" label="分组" width="110">
          <template #default="{ row }">{{ row.group_name || '—' }}</template>
        </el-table-column>
        <el-table-column prop="metric" label="指标" width="100" />
        <el-table-column prop="level" label="级别" width="80" />
        <el-table-column prop="rule_name" label="规则" min-width="130">
          <template #default="{ row }">{{ row.rule_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="通道" width="100">
          <template #default="{ row }">{{ channelLabels[row.channel] || row.channel }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">
              {{ statusLabels[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="70">
          <template #default="{ row }">{{ row.attempts }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ row.duration_ms }} ms</template>
        </el-table-column>
        <el-table-column label="最后错误" min-width="200">
          <template #default="{ row }">
            <span v-if="row.error">{{ row.error }}</span>
            <span v-else-if="row.status === 'pending'" class="muted">等待重试</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="下次重试" width="170">
          <template #default="{ row }">
            {{ row.next_attempt_at ? formatDateTime(row.next_attempt_at) : '—' }}
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        class="pager"
        layout="total, sizes, prev, pager, next"
        :total="total"
        :current-page="filters.page"
        :page-size="filters.pageSize"
        :page-sizes="[20, 50, 100]"
        @current-change="handlePageChange"
        @size-change="handleSizeChange"
      />
    </el-card>
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
  color: #64748b;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}

.filter-error {
  margin-bottom: 12px;
}

.muted {
  color: #94a3b8;
}

.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
</style>
