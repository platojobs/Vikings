<script setup lang="ts">
/**
 * OTA 管理：
 * - 创建任务（多选设备 / 目标版本 / 固件 URL / 备注）→ POST /api/ota/tasks
 * - 任务列表 → GET /api/ota/tasks，展开行展示每台设备的升级进度
 * - 订阅 device/+/cmd/ack，按 cmdId 定位设备并实时刷新进度条
 */
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchDevices } from '@/api/devices'
import { createOtaTask, fetchOtaTasks } from '@/api/ota'
import { subscribe } from '@/services/mqtt'
import { formatDateTime } from '@/utils/format'
import { commandStateLabel, commandStateTagType, progressStatus } from '@/utils/labels'
import type { CommandAckMessage, Device, OtaTask } from '@/types'

const devices = ref<Device[]>([])
const devicesLoading = ref(false)

const form = reactive({
  deviceIds: [] as string[],
  targetVersion: '',
  url: '',
  description: '',
})
const submitting = ref(false)

const tasks = ref<OtaTask[]>([])
const tasksLoading = ref(false)
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

let offAck: (() => void) | null = null

async function loadDevices(): Promise<void> {
  devicesLoading.value = true
  try {
    const result = await fetchDevices({ page: 1, pageSize: 200 })
    devices.value = result.items
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    devicesLoading.value = false
  }
}

async function loadTasks(): Promise<void> {
  tasksLoading.value = true
  try {
    const result = await fetchOtaTasks(page.value, pageSize.value)
    tasks.value = result.items
    total.value = result.total
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    tasksLoading.value = false
  }
}

async function handleSubmit(): Promise<void> {
  if (form.deviceIds.length === 0) {
    ElMessage.warning('请至少选择一台目标设备')
    return
  }
  if (!form.targetVersion.trim()) {
    ElMessage.warning('请填写目标版本')
    return
  }
  if (!form.url.trim()) {
    ElMessage.warning('请填写固件 URL')
    return
  }

  submitting.value = true
  try {
    await createOtaTask({
      deviceIds: [...form.deviceIds],
      targetVersion: form.targetVersion.trim(),
      url: form.url.trim(),
      description: form.description.trim() || undefined,
    })
    ElMessage.success('OTA 任务创建成功，等待设备回执')
    form.deviceIds = []
    form.targetVersion = ''
    form.url = ''
    form.description = ''
    page.value = 1
    await loadTasks()
  } catch {
    // 错误提示已由 HTTP 层统一处理
  } finally {
    submitting.value = false
  }
}

/** 回执按 cmdId 匹配到任务内的设备，就地更新状态与进度 */
function handleAck(payload: unknown): void {
  const ack = payload as CommandAckMessage
  if (!ack?.cmdId) return

  const task = tasks.value.find((item) =>
    item.devices.some((device) => device.cmd_id === ack.cmdId),
  )
  if (!task) return

  const target = task.devices.find((device) => device.cmd_id === ack.cmdId)
  if (!target) return

  target.state = ack.state
  if (typeof ack.progress === 'number') target.progress = ack.progress
  if (ack.message) target.message = ack.message

  // 汇总数同步刷新，保证任务卡片统计与设备进度一致
  task.success = task.devices.filter((device) => device.state === 'success').length
  task.failed = task.devices.filter((device) => device.state === 'failed').length
}

function taskPercent(task: OtaTask): number {
  if (!task.total) return 0
  return Math.round(((task.success + task.failed) / task.total) * 100)
}

function handleSizeChange(): void {
  page.value = 1
  loadTasks()
}

onMounted(async () => {
  offAck = subscribe('device/+/cmd/ack', handleAck)
  await Promise.all([loadDevices(), loadTasks()])
})

onBeforeUnmount(() => offAck?.())
</script>

<template>
  <div class="page ota-page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">创建 OTA 任务</span>
          <span class="header-hint">创建后网关逐台下发指令，设备在唤醒窗口内回执进度</span>
        </div>
      </template>

      <el-form label-position="top" class="ota-form" @submit.prevent="handleSubmit">
        <el-row :gutter="16">
          <el-col :span="24">
            <el-form-item label="目标设备（可多选）">
              <el-select
                v-model="form.deviceIds"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                placeholder="请选择需要升级的设备"
                :loading="devicesLoading"
                class="device-select"
              >
                <el-option
                  v-for="device in devices"
                  :key="device.device_id"
                  :label="`${device.name}（${device.device_id} · ${device.firmware_version}）`"
                  :value="device.device_id"
                />
              </el-select>
            </el-form-item>
          </el-col>

          <el-col :xs="24" :md="8">
            <el-form-item label="目标版本">
              <el-input v-model="form.targetVersion" placeholder="如 1.1.0" />
            </el-form-item>
          </el-col>

          <el-col :xs="24" :md="16">
            <el-form-item label="固件 URL">
              <el-input v-model="form.url" placeholder="https://example.com/fw-1.1.0.bin" />
            </el-form-item>
          </el-col>

          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="form.description" placeholder="可选，说明本次升级范围" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-button type="primary" :loading="submitting" @click="handleSubmit">
          创建 OTA 任务
        </el-button>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">任务列表</span>
          <div class="header-meta">
            <span class="header-hint">已订阅 device/+/cmd/ack，进度实时刷新</span>
            <el-button size="small" :loading="tasksLoading" @click="loadTasks">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="tasksLoading"
        :data="tasks"
        row-key="task_id"
        class="task-table"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="ota-device-list">
              <div v-for="item in row.devices" :key="item.cmd_id" class="ota-device">
                <div class="ota-device-head">
                  <span class="ota-device-id">{{ item.device_id }}</span>
                  <el-tag :type="commandStateTagType(item.state)" size="small">
                    {{ commandStateLabel(item.state) }}
                  </el-tag>
                  <span class="ota-progress-text">{{ item.progress }}%</span>
                </div>
                <el-progress
                  :percentage="item.progress"
                  :status="progressStatus(item.state)"
                  :stroke-width="10"
                />
                <div class="ota-device-message">
                  {{ item.message || '等待设备回执' }} · 指令 {{ item.cmd_id }}
                </div>
              </div>
              <el-empty
                v-if="!row.devices || row.devices.length === 0"
                description="暂无设备升级状态"
                :image-size="60"
              />
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="task_id" label="任务 ID" min-width="150" show-overflow-tooltip />
        <el-table-column prop="target_version" label="目标版本" width="110" />
        <el-table-column prop="url" label="固件 URL" min-width="220" show-overflow-tooltip />
        <el-table-column label="升级进度" min-width="190">
          <template #default="{ row }">
            <div class="task-progress">
              <div class="task-progress-text">
                成功 {{ row.success }} / 失败 {{ row.failed }} / 共 {{ row.total }} 台
              </div>
              <el-progress :percentage="taskPercent(row)" :stroke-width="6" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无 OTA 任务" :image-size="80" />
        </template>
      </el-table>

      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="loadTasks"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}

.header-hint {
  font-size: 12px;
  color: #94a3b8;
}

.header-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ota-form :deep(.el-form-item) {
  margin-bottom: 16px;
}

.device-select {
  width: 100%;
}

.ota-device-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 48px 16px;
}

.ota-device {
  padding: 12px 14px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
}

.ota-device-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.ota-device-id {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
}

.ota-progress-text {
  margin-left: auto;
  font-size: 13px;
  font-weight: 600;
  color: #2563eb;
}

.ota-device-message {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
}

.task-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-progress-text {
  font-size: 12px;
  color: #64748b;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}
</style>
