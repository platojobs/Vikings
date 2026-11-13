<script setup lang="ts">
import { computed } from 'vue'
import { isConditionNode } from '@/types'
import type { AlertRuleCondition, AlertRuleConditionNode } from '@/types'
import { MAX_CONDITION_DEPTH } from '@/utils/conditions'

defineOptions({ name: 'RuleConditionBuilder' })

const props = withDefaults(
  defineProps<{
    modelValue: AlertRuleConditionNode
    depth?: number
    groupOptions?: string[]
    tagOptions?: string[]
    deviceOptions?: string[]
  }>(),
  {
    depth: 1,
    groupOptions: () => [],
    tagOptions: () => [],
    deviceOptions: () => [],
  },
)

const emit = defineEmits<{ 'update:modelValue': [AlertRuleConditionNode] }>()

const node = computed(() => props.modelValue)
const canNest = computed(() => props.depth < MAX_CONDITION_DEPTH)

function patch(next: AlertRuleConditionNode): void {
  emit('update:modelValue', next)
}

function setOp(op: 'all' | 'any'): void {
  patch({ ...node.value, op })
}

function patchChild(index: number, next: AlertRuleCondition): void {
  const children = [...node.value.children]
  children[index] = next
  patch({ ...node.value, children })
}

function removeChild(index: number): void {
  patch({ ...node.value, children: node.value.children.filter((_, i) => i !== index) })
}

function addLeaf(): void {
  patch({ ...node.value, children: [...node.value.children, { dim: 'tag', values: [] }] })
}

function addGroup(): void {
  patch({
    ...node.value,
    children: [...node.value.children, { op: 'all', children: [{ dim: 'tag', values: [] }] }],
  })
}

function setDim(index: number, dim: 'group' | 'device' | 'tag'): void {
  // 换维度时清空值：分组名 / 设备 ID / 标签不是同一命名空间，保留会误配
  patchChild(index, { dim, values: [] })
}

function setValues(index: number, values: string[]): void {
  const child = node.value.children[index]
  if (!child || isConditionNode(child)) return
  patchChild(index, { dim: child.dim, values: values.map((value) => value.trim()).filter(Boolean) })
}

function optionsFor(dim: 'group' | 'device' | 'tag'): string[] {
  if (dim === 'group') return props.groupOptions
  if (dim === 'device') return props.deviceOptions
  return props.tagOptions
}
</script>

<template>
  <div class="condition-node" :class="{ 'condition-node--nested': depth > 1 }">
    <div class="condition-node__head">
      <el-radio-group
        :model-value="node.op"
        size="small"
        :data-test="`condition-op-${depth}`"
        @update:model-value="setOp($event as 'all' | 'any')"
      >
        <el-radio-button value="all">全部满足（且）</el-radio-button>
        <el-radio-button value="any">任一满足（或）</el-radio-button>
      </el-radio-group>
      <span class="condition-node__hint">同一条件内多个值为「或」；条件组按左侧规则组合</span>
    </div>

    <div v-for="(child, index) in node.children" :key="index" class="condition-row">
      <template v-if="isConditionNode(child)">
        <RuleConditionBuilder
          :model-value="child"
          :depth="depth + 1"
          :group-options="groupOptions"
          :tag-options="tagOptions"
          :device-options="deviceOptions"
          class="condition-row__nested"
          @update:model-value="patchChild(index, $event)"
        />
        <el-button link type="danger" @click="removeChild(index)">移除条件组</el-button>
      </template>
      <template v-else>
        <el-select
          :model-value="child.dim"
          class="condition-row__dim"
          size="small"
          @update:model-value="setDim(index, $event as 'group' | 'device' | 'tag')"
        >
          <el-option label="分组" value="group" />
          <el-option label="设备" value="device" />
          <el-option label="标签" value="tag" />
        </el-select>
        <el-select
          :model-value="child.values"
          class="condition-row__values"
          size="small"
          multiple
          filterable
          allow-create
          default-first-option
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择或输入值，可填多个"
          :data-test="`condition-values-${depth}-${index}`"
          @update:model-value="setValues(index, $event as string[])"
        >
          <el-option v-for="option in optionsFor(child.dim)" :key="option" :label="option" :value="option" />
        </el-select>
        <el-button link type="danger" @click="removeChild(index)">移除</el-button>
      </template>
    </div>

    <div class="condition-node__actions">
      <el-button size="small" :data-test="`condition-add-leaf-${depth}`" @click="addLeaf">
        添加条件
      </el-button>
      <el-button
        size="small"
        :disabled="!canNest"
        :data-test="`condition-add-group-${depth}`"
        @click="addGroup"
      >
        添加条件组
      </el-button>
      <span v-if="!canNest" class="condition-node__limit">
        最多嵌套 {{ MAX_CONDITION_DEPTH }} 层
      </span>
    </div>
  </div>
</template>

<style scoped>
.condition-node {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
}

.condition-node--nested {
  background: var(--el-fill-color-lighter);
}

.condition-node__head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.condition-node__hint,
.condition-node__limit {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.condition-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
}

.condition-row__nested {
  flex: 1 1 420px;
}

.condition-row__dim {
  width: 110px;
}

.condition-row__values {
  flex: 1 1 320px;
  min-width: 220px;
}

.condition-node__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
