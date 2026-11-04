<script setup lang="ts">
/**
 * 审计日志：谁在什么时间对什么资源做了什么操作。
 * 支持按操作人 / 动作类型筛选，detail JSON 可展开查看。
 */
import { onMounted, reactive, ref } from 'vue'
import { fetchAuditLogs } from '@/api/audit'
import { formatDateTime } from '@/utils/format'
import { auditActionLabel, auditActionTagType } from '@/utils/labels'
import type { AuditLog } from '@/types'

const ACTION_OPTIONS = [
  { label: '登录', value: 'login' },
  { label: '下发指令', value: 'command.create' },
  { label: '创建 OTA 任务', value: 'ota.create' },
  { label: '确认告警', value: 'alert.ack' },
  { label: '修改阈值', value: 'threshold.update' },
  { label: '新建账号', value: 'user.create' },
  { label: '修改账号', value: 'user.update' },
  { label: '删除账号', value: 'user.delete' },
]

const query = reactive({ operator: '', action: '' })

const logs = ref<AuditLog[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

async function loadLogs(): Promise<void> {
  loading.value = true
  try {
    const result = await fetchAuditLogs({
      operator: query.operator.trim() || undefined,
      action: query.action || undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    logs.value = result.items
    total.value = result.total
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  loadLogs()
}

function handleReset(): void {
  query.operator = ''
  query.action = ''
  page.value = 1
  loadLogs()
}

function handleSizeChange(): void {
  page.value = 1
  loadLogs()
}

/** detail 展示为紧凑 JSON，空对象显示占位符 */
function formatDetail(detail: Record<string, unknown>): string {
  const keys = Object.keys(detail || {})
  if (keys.length === 0) return '—'
  return JSON.stringify(detail)
}

/** 目标列：target_type + target_id 拼接，缺省显示占位符 */
function formatTarget(row: AuditLog): string {
  if (!row.target_type) return '—'
  return row.target_id ? `${row.target_type} / ${row.target_id}` : row.target_type
}

onMounted(loadLogs)
</script>

<template>
  <div class="page audit-page">
    <el-card shadow="never">
      <div class="filter-bar">
        <el-input
          v-model="query.operator"
          class="operator-input"
          placeholder="按操作人筛选"
          clearable
          @keyup.enter="handleSearch"
        />
        <el-select v-model="query.action" placeholder="全部动作" clearable class="action-select">
          <el-option
            v-for="item in ACTION_OPTIONS"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
        <span class="total-hint">共 {{ total }} 条记录</span>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="logs" row-key="id">
        <el-table-column prop="operator" label="操作人" width="120" />
        <el-table-column label="动作" width="140">
          <template #default="{ row }">
            <el-tag :type="auditActionTagType(row.action)" size="small" effect="plain">
              {{ auditActionLabel(row.action) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作目标" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ formatTarget(row) }}</template>
        </el-table-column>
        <el-table-column label="详情" min-width="280">
          <template #default="{ row }">
            <el-tooltip
              v-if="Object.keys(row.detail || {}).length"
              :content="formatDetail(row.detail)"
              placement="top"
              :show-after="200"
              max-width="480px"
            >
              <code class="detail-code">{{ formatDetail(row.detail) }}</code>
            </el-tooltip>
            <span v-else class="detail-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无审计记录" :image-size="80" />
        </template>
      </el-table>

      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="loadLogs"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.operator-input {
  width: 180px;
}

.action-select {
  width: 160px;
}

.total-hint {
  font-size: 13px;
  color: #94a3b8;
}

.table-card {
  margin-top: 16px;
}

.detail-code {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
  font-size: 12px;
  color: #475569;
  background: #f1f5f9;
  border-radius: 4px;
  padding: 2px 6px;
}

.detail-empty {
  color: #cbd5e1;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
