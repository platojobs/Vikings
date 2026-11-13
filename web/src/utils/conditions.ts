import { isConditionNode } from '@/types'
import type {
  AlertRule,
  AlertRuleCondition,
  AlertRuleConditionLeaf,
  AlertRuleConditionNode,
} from '@/types'

/** 与后端 `rule_conditions.MAX_DEPTH` 保持一致（含最外层） */
export const MAX_CONDITION_DEPTH = 3
/** 与后端 `rule_conditions.MAX_LEAVES` 保持一致 */
export const MAX_CONDITION_LEAVES = 20

const DIM_LABEL: Record<AlertRuleConditionLeaf['dim'], string> = {
  group: '分组',
  device: '设备',
  tag: '标签',
}

/** 深拷贝，避免编辑草稿直接改到列表里的规则对象 */
export function cloneConditions(node: AlertRuleConditionNode): AlertRuleConditionNode {
  return JSON.parse(JSON.stringify(node)) as AlertRuleConditionNode
}

/** 新建条件时的默认骨架：一个空标签条件，等用户选择/输入值 */
export function emptyConditions(): AlertRuleConditionNode {
  return {
    op: 'all',
    children: [{ dim: 'tag', values: [] }],
  }
}

/**
 * 把规则转成编辑器里的等价条件树。
 *
 * v1.35 及以前的范围（global / 单维度多值）没有 conditions，
 * 打开老规则时按老字段生成等价树，而不是显示空白。
 */
export function conditionFromRule(rule: AlertRule): AlertRuleConditionNode | null {
  if (rule.conditions) return cloneConditions(rule.conditions)
  if (rule.scope_type === 'global') return null
  if (rule.scope_type === 'custom') return emptyConditions()
  const values = rule.scope_values?.length
    ? [...rule.scope_values]
    : rule.scope_value
      ? [rule.scope_value]
      : []
  if (!values.length) return null
  return {
    op: 'any',
    children: [{ dim: rule.scope_type, values }],
  }
}

/** 人类可读的条件描述；嵌套节点加括号，便于表格与历史里快速对照 */
export function describeConditions(node: AlertRuleCondition): string {
  if (!isConditionNode(node)) {
    const label = DIM_LABEL[node.dim] ?? node.dim
    return `${label}：${node.values.length ? node.values.join(' / ') : '（未配置）'}`
  }
  if (!node.children.length) return '（空条件）'
  const joiner = node.op === 'all' ? ' 且 ' : ' 或 '
  return node.children
    .map((child) => {
      const text = describeConditions(child)
      return isConditionNode(child) && child.children.length > 1 ? `(${text})` : text
    })
    .join(joiner)
}

export function countConditionLeaves(node: AlertRuleCondition): number {
  if (!isConditionNode(node)) return 1
  return node.children.reduce((total, child) => total + countConditionLeaves(child), 0)
}

/** 条件组层数（含最外层）；叶子不计入，与后端 `MAX_DEPTH` 的计数方式一致 */
export function conditionDepth(node: AlertRuleCondition, depth = 1): number {
  if (!isConditionNode(node)) return depth - 1
  const nested = node.children.filter(isConditionNode)
  if (!nested.length) return depth
  return Math.max(...nested.map((child) => conditionDepth(child, depth + 1)))
}

/**
 * 客户端预校验：服务端仍会再校验一次。
 * 返回可读错误文案，`null` 表示通过。
 */
export function validateConditions(node: AlertRuleConditionNode): string | null {
  if (conditionDepth(node) > MAX_CONDITION_DEPTH) {
    return `条件树最多嵌套 ${MAX_CONDITION_DEPTH} 层（含最外层）`
  }
  if (countConditionLeaves(node) > MAX_CONDITION_LEAVES) {
    return `单条规则最多 ${MAX_CONDITION_LEAVES} 个条件`
  }
  let error: string | null = null
  const walk = (current: AlertRuleCondition): void => {
    if (error) return
    if (!isConditionNode(current)) {
      if (!current.values.length || current.values.some((value) => !value.trim())) {
        error = `${DIM_LABEL[current.dim] ?? current.dim}条件至少填写一个值`
      }
      return
    }
    if (!current.children.length) {
      error = '条件组至少需要一个子条件'
      return
    }
    current.children.forEach(walk)
  }
  walk(node)
  return error
}
