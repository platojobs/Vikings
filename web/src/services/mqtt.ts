/**
 * MQTT over WebSocket 单例封装。
 *
 * 浏览器无法直连 TCP（1883），因此连接 Mosquitto 的 9001 WebSocket 端口。
 * Broker 已关闭匿名：连接前必须用登录 JWT 调 /api/auth/mqtt-credentials
 * 换取 `web-console` 只读凭证（口令只存在于服务端 env，不打进前端产物）。
 * - clientId 随机生成，避免多标签页/重复连接被 Broker 踢掉
 * - 断线依赖 mqtt.js 自带 reconnectPeriod 自动重连
 * - 订阅关系集中登记，重连成功后自动重新订阅
 */

import mqtt, { type MqttClient } from 'mqtt'
import { request } from '@/api/http'
import type { MqttCredential } from '@/types'

type MessageHandler = (payload: unknown, topic: string) => void
type StatusHandler = (connected: boolean) => void

/** topicFilter -> 处理器集合（同一 Topic 允许多个页面同时订阅） */
const subscriptions = new Map<string, Set<MessageHandler>>()
const statusHandlers = new Set<StatusHandler>()

let client: MqttClient | null = null
let connected = false
let initStarted = false

/** 通配符匹配：支持 `+`（单层）与 `#`（多层结尾） */
function topicMatches(pattern: string, topic: string): boolean {
  const patternParts = pattern.split('/')
  const topicParts = topic.split('/')
  for (let i = 0; i < patternParts.length; i += 1) {
    const part = patternParts[i]
    if (part === '#') return true
    if (i >= topicParts.length) return false
    if (part === '+') continue
    if (part !== topicParts[i]) return false
  }
  return patternParts.length === topicParts.length
}

function notifyStatus(next: boolean): void {
  if (connected === next) return
  connected = next
  statusHandlers.forEach((handler) => handler(connected))
}

/**
 * 解析 MQTT WebSocket 地址。
 * - 未配置：开发默认 ws://127.0.0.1:9001
 * - 以 `/` 开头（生产构建传 `/mqtt`）：按当前页面同源拼成 ws(s)://<host>/mqtt，
 *   由反代转发到 Broker 的 WebSocket 监听，页面无需知道内部端口。
 */
function resolveMqttUrl(configured?: string): string {
  const value = configured?.trim()
  if (!value) return 'ws://127.0.0.1:9001'
  if (value.startsWith('/')) {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${window.location.host}${value}`
  }
  return value
}

/** 懒创建连接，全应用只保留一个 client；先换取只读凭证再连 Broker */
function ensureClient(): void {
  if (client || initStarted) return
  initStarted = true
  void initClient()
}

async function initClient(): Promise<void> {
  let credential: MqttCredential
  try {
    // 走统一 HTTP 封装：access 过期时自动单飞刷新，避免长连接重连时被 401 挡住
    credential = await request<MqttCredential>('/api/auth/mqtt-credentials?client=web', {
      silent: true,
    })
  } catch (error) {
    // 未登录 / 凭证未配置时不匿名连接：置为未连接，稍后重试（登录页本身不需要 MQTT）
    initStarted = false
    notifyStatus(false)
    console.warn('[MQTT] 获取只读凭证失败，不进行匿名降级：', error)
    window.setTimeout(() => ensureClient(), 5_000)
    return
  }

  const url = resolveMqttUrl(import.meta.env.VITE_MQTT_WS)
  const clientId = `web-console-${Math.random().toString(16).slice(2, 10)}-${Date.now().toString(36)}`

  client = mqtt.connect(url, {
    clientId,
    username: credential.username,
    password: credential.password,
    clean: true,
    connectTimeout: 10_000,
    reconnectPeriod: 4_000,
    keepalive: 60,
  })

  client.on('connect', () => {
    notifyStatus(true)
    // 重连后 Broker 侧订阅已丢失，需要按登记表重新订阅
    subscriptions.forEach((_handlers, pattern) => {
      client?.subscribe(pattern, { qos: 1 })
    })
  })

  client.on('close', () => notifyStatus(false))
  client.on('offline', () => notifyStatus(false))
  client.on('error', (error: Error) => {
    console.warn('[MQTT] 连接异常：', error.message)
  })

  client.on('message', (topic, payload) => {
    let data: unknown
    try {
      data = JSON.parse(payload.toString('utf8'))
    } catch {
      console.warn('[MQTT] 无法解析的报文：', topic)
      return
    }
    subscriptions.forEach((handlers, pattern) => {
      if (!topicMatches(pattern, topic)) return
      handlers.forEach((handler) => {
        try {
          handler(data, topic)
        } catch (error) {
          console.warn('[MQTT] 回调执行失败：', error)
        }
      })
    })
  })
}

/**
 * 订阅 Topic，返回取消订阅函数。
 * 组件卸载时务必调用返回值，避免重复回调。
 */
export function subscribe(pattern: string, handler: MessageHandler): () => void {
  let handlers = subscriptions.get(pattern)
  if (!handlers) {
    handlers = new Set<MessageHandler>()
    subscriptions.set(pattern, handlers)
    ensureClient()
    if (client?.connected) {
      client.subscribe(pattern, { qos: 1 })
    }
  }
  handlers.add(handler)

  return () => {
    const current = subscriptions.get(pattern)
    if (!current) return
    current.delete(handler)
    if (current.size === 0) {
      subscriptions.delete(pattern)
      client?.unsubscribe(pattern)
    }
  }
}

/**
 * 监听连接状态，返回取消监听函数。
 * 注册时会立即回调一次当前状态，方便页面初始化展示。
 */
export function onStatusChange(handler: StatusHandler): () => void {
  statusHandlers.add(handler)
  ensureClient()
  handler(connected)
  return () => {
    statusHandlers.delete(handler)
  }
}
