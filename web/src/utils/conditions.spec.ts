import { describe, expect, it } from 'vitest'
import {
  conditionDepth,
  conditionFromRule,
  countConditionLeaves,
  describeConditions,
  emptyConditions,
  validateConditions,
} from '@/utils/conditions'
import type { AlertRule, AlertRuleConditionNode } from '@/types'

/**
 * 覆盖范围（v1.38.0）：
 * - 老规则（global / 单维度多值）打开时生成等价条件树，而不是空白；
 * - 条件描述能表达「(标签A 或 标签B) 且 分组C」；
 * - 客户端预校验与服务端限制一致（空值、深度、叶子数）。
 */

const baseRule: AlertRule = {
  id: 1,
  name: '测试规则',
  metric: 'voltage',
  op: 'lt',
  threshold: 3.4,
  level: 'warning',
  scope_type: 'global',
  scope_value: null,
  scope_values: null,
  enabled: true,
  consecutive_count: 1,
  priority: 100,
  is_default: false,
  updated_by: 'tester',
}

describe('条件树与老范围等价转换', () => {
  it('global 规则没有条件树', () => {
    expect(conditionFromRule(baseRule)).toBeNull()
  })

  it('单维度多值老规则转换为单叶子 any', () => {
    expect(
      conditionFromRule({ ...baseRule, scope_type: 'tag', scope_value: '冷库', scope_values: ['冷库', '关键'] }),
    ).toEqual({
      op: 'any',
      children: [{ dim: 'tag', values: ['冷库', '关键'] }],
    })
  })

  it('只有 scope_value 的 v1.34 规则也能转换', () => {
    expect(
      conditionFromRule({ ...baseRule, scope_type: 'group', scope_value: '华东', scope_values: null }),
    ).toEqual({
      op: 'any',
      children: [{ dim: 'group', values: ['华东'] }],
    })
  })

  it('已有 conditions 时原样深拷贝，不共享引用', () => {
    const conditions: AlertRuleConditionNode = {
      op: 'all',
      children: [{ dim: 'group', values: ['华东'] }],
    }
    const result = conditionFromRule({ ...baseRule, scope_type: 'custom', conditions })
    expect(result).toEqual(conditions)
    expect(result).not.toBe(conditions)
  })
})

describe('条件描述', () => {
  it('嵌套条件按且/或并加括号', () => {
    const conditions: AlertRuleConditionNode = {
      op: 'all',
      children: [
        {
          op: 'any',
          children: [
            { dim: 'tag', values: ['冷库'] },
            { dim: 'tag', values: ['关键'] },
          ],
        },
        { dim: 'group', values: ['华东'] },
      ],
    }
    expect(describeConditions(conditions)).toBe('(标签：冷库 或 标签：关键) 且 分组：华东')
  })

  it('单值条件不额外加括号', () => {
    expect(
      describeConditions({
        op: 'all',
        children: [
          { dim: 'tag', values: ['冷库'] },
          { dim: 'group', values: ['华东'] },
        ],
      }),
    ).toBe('标签：冷库 且 分组：华东')
  })
})

describe('条件预校验', () => {
  it('默认骨架是合法结构但值为空，保存前会被拦截', () => {
    const node = emptyConditions()
    expect(validateConditions(node)).toContain('至少填写一个值')
  })

  it('空条件组被拒绝', () => {
    expect(validateConditions({ op: 'all', children: [] })).toContain('至少需要一个子条件')
  })

  it('超过 3 层被拒绝，3 层内通过', () => {
    const three: AlertRuleConditionNode = {
      op: 'all',
      children: [
        {
          op: 'any',
          children: [
            { op: 'all', children: [{ dim: 'tag', values: ['冷库'] }] },
          ],
        },
      ],
    }
    expect(conditionDepth(three)).toBe(3)
    expect(validateConditions(three)).toBeNull()

    const four: AlertRuleConditionNode = {
      op: 'all',
      children: [
        {
          op: 'any',
          children: [
            {
              op: 'all',
              children: [{ op: 'any', children: [{ dim: 'tag', values: ['冷库'] }] }],
            },
          ],
        },
      ],
    }
    expect(validateConditions(four)).toContain('最多嵌套 3 层')
  })

  it('叶子数为 1 的嵌套结构与多值叶子统计正确', () => {
    const node: AlertRuleConditionNode = {
      op: 'all',
      children: [
        { dim: 'tag', values: ['冷库', '关键'] },
        { dim: 'group', values: ['华东'] },
      ],
    }
    expect(countConditionLeaves(node)).toBe(2)
  })
})
