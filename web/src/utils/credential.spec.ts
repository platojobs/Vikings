import { describe, expect, it } from 'vitest'
import {
  CREDENTIAL_FINGERPRINT_NOTE,
  ONE_TIME_PASSWORD_NOTICE,
  UNISSUED_CREDENTIAL_NOTICE,
  buildRotationImpactText,
  credentialStatusLabel,
  useOneTimeCredential,
} from '@/utils/credential'

describe('设备凭证展示口径', () => {
  it('未签发/已签发/已吊销三种状态文案明确', () => {
    expect(credentialStatusLabel('missing')).toBe('未签发')
    expect(credentialStatusLabel('active')).toBe('已签发')
    expect(credentialStatusLabel('revoked')).toBe('已吊销')
    expect(UNISSUED_CREDENTIAL_NOTICE).toContain('无法接入 Broker')
  })

  it('指纹说明写清只展示短前缀与用途', () => {
    expect(CREDENTIAL_FINGERPRINT_NOTE).toContain('前 8 位')
    expect(CREDENTIAL_FINGERPRINT_NOTE).toContain('对账')
    expect(CREDENTIAL_FINGERPRINT_NOTE).toContain('不会返回完整指纹')
  })

  it('轮换二次确认写清旧凭证失效、设备需改配置、Broker 重载失败三种影响', () => {
    const text = buildRotationImpactText('dev-1001')
    expect(text).toContain('dev-1001')
    expect(text).toContain('旧凭证对新连接立即失效')
    expect(text).toContain('已建立的连接可能维持到下次断线重连')
    expect(text).toContain('设备必须更新配置中的口令')
    expect(text).toContain('Broker 重载失败')
    expect(text).toContain('旧凭证可能仍然有效')
    expect(text).toContain('关闭后无法再查询')
  })
})

describe('一次性口令生命周期', () => {
  it('展示一次后确认关闭即清空且不可再查', () => {
    const secret = useOneTimeCredential()
    expect(secret.visible.value).toBe(false)
    expect(secret.password.value).toBeNull()

    secret.show('one-time-password-value')
    expect(secret.visible.value).toBe(true)
    expect(secret.password.value).toBe('one-time-password-value')

    secret.acknowledge()
    expect(secret.visible.value).toBe(false)
    expect(secret.password.value).toBeNull()
    expect(secret.canReopen.value).toBe(false)
  })

  it('一次性口令提示说明服务端只存指纹', () => {
    expect(ONE_TIME_PASSWORD_NOTICE).toContain('服务端只保存 SHA-256 指纹')
    expect(ONE_TIME_PASSWORD_NOTICE).toContain('关闭后无法再次查询')
  })
})
