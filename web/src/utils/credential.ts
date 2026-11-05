/**
 * 设备 MQTT 凭证的展示与一次性口令状态。
 *
 * 服务端只保存口令指纹，轮换响应里的 password 是唯一一次明文机会；
 * 这里把「展示 → 确认已保存 → 清空」收敛为一个可测试的状态，避免误留明文。
 */
import { ComputedRef, Ref, computed, ref } from 'vue'
import type { MqttCredentialStatus } from '@/types'

export interface OneTimeCredential {
  password: Ref<string | null>
  visible: Ref<boolean>
  show: (password: string) => void
  acknowledge: () => void
  /** 关闭后永远为 false：只能通过新的轮换重新获得口令 */
  canReopen: ComputedRef<boolean>
}

export function useOneTimeCredential(): OneTimeCredential {
  const password = ref<string | null>(null)
  const visible = ref(false)
  const show = (value: string): void => {
    password.value = value
    visible.value = true
  }
  const acknowledge = (): void => {
    password.value = null
    visible.value = false
  }
  return {
    password,
    visible,
    show,
    acknowledge,
    canReopen: computed(() => false),
  }
}

export function credentialStatusLabel(status: MqttCredentialStatus): string {
  switch (status) {
    case 'active':
      return '已签发'
    case 'revoked':
      return '已吊销'
    default:
      return '未签发'
  }
}

export function credentialStatusTagType(
  status: MqttCredentialStatus,
): 'success' | 'danger' | 'info' {
  if (status === 'active') return 'success'
  if (status === 'revoked') return 'danger'
  return 'info'
}

/** 二次确认必须写清真实影响，不能只说「确定轮换吗」 */
export function buildRotationImpactText(deviceId: string): string {
  return [
    `确认轮换设备 ${deviceId} 的 MQTT 凭证？`,
    '',
    '影响：',
    '1. 旧凭证对新连接立即失效；已建立的连接可能维持到下次断线重连，设备在重连前可能仍显示在线；',
    '2. 设备必须更新配置中的口令后才能重连，未更新前一旦断线将持续离线；',
    '3. 若 Broker 重载失败，新口令已写入但旧凭证可能仍然有效，需按提示人工重载 Broker。',
    '',
    '新口令只在下一步显示一次，服务端只保存指纹，关闭后无法再查询。',
  ].join('\n')
}

export function buildIssueImpactText(deviceId: string): string {
  return [
    `确认为设备 ${deviceId} 签发 MQTT 凭证？`,
    '',
    '签发后设备即可使用该用户名/口令接入 Broker；新口令只显示一次。',
  ].join('\n')
}

export const CREDENTIAL_FINGERPRINT_NOTE =
  '仅展示指纹前 8 位，用于人工对账；服务端不保存口令明文，也不会返回完整指纹。'

export const UNISSUED_CREDENTIAL_NOTICE = '该设备尚未签发凭证，无法接入 Broker'

export const ONE_TIME_PASSWORD_NOTICE =
  '口令只显示这一次：服务端只保存 SHA-256 指纹，关闭后无法再次查询，如需新口令只能重新轮换。'
