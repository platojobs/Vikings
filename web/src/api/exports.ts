/**
 * 报表导出（v1.30.0）：直接下载服务端流式生成的 CSV。
 *
 * 前端不拼 CSV、也不翻页拉全量：只发一次带筛选参数的请求，边接收边写入 Blob。
 * 下载过程中通过 `onProgress` 回报已接收字节数；后端未提供 Content-Length 时
 * （流式响应常见）只显示已下载体积，不假装知道百分比。
 */

import { buildApiUrl, refreshAccessToken } from '@/api/http'
import { clearAuth, getToken } from '@/utils/auth'

export interface ExportProgress {
  loadedBytes: number
  totalBytes?: number
}

export interface ExportResult {
  filename: string
  bytes: number
}

interface ExportParams {
  /** v1.35.0：数组按重复参数序列化（tag=a&tag=b） */
  [key: string]: string | number | string[] | undefined
}

class ExportError extends Error {
  status: number

  constructor(message: string, status = -1) {
    super(message)
    this.name = 'ExportError'
    this.status = status
  }
}

/** 从 Content-Disposition 提取文件名；优先 RFC 5987，其次普通 filename */
export function parseFilename(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim())
    } catch {
      return encoded[1].trim()
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain ? plain[1].trim() : fallback
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { message?: string; detail?: string }
    return payload.message || payload.detail || `导出失败（HTTP ${response.status}）`
  } catch {
    return `导出失败（HTTP ${response.status}）`
  }
}

async function fetchOnce(
  url: string,
  token: string | null,
): Promise<Response> {
  return fetch(url, {
    method: 'GET',
    headers: {
      Accept: 'text/csv',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  })
}

/**
 * 下载一份导出文件。失败时抛 `ExportError`（message 可直接展示给用户），
 * 401 会先尝试单飞刷新再重放一次。
 */
export async function downloadExport(
  path: string,
  params: ExportParams,
  fallbackFilename: string,
  onProgress?: (progress: ExportProgress) => void,
): Promise<ExportResult> {
  const url = buildApiUrl(path, params)
  let token = getToken()
  let response = await fetchOnce(url, token)

  if (response.status === 401) {
    const refreshed = await refreshAccessToken()
    if (!refreshed) {
      clearAuth()
      throw new ExportError('登录已过期，请重新登录', 401)
    }
    token = getToken()
    response = await fetchOnce(url, token)
  }

  if (!response.ok) {
    throw new ExportError(await readError(response), response.status)
  }

  const totalHeader = response.headers.get('Content-Length')
  const totalBytes = totalHeader ? Number(totalHeader) : undefined
  const chunks: Uint8Array[] = []
  let loadedBytes = 0

  const reader = response.body?.getReader()
  if (!reader) {
    // 极旧浏览器兜底：直接整包下载，此时无法回报中间进度
    const blob = await response.blob()
    const filename = parseFilename(
      response.headers.get('Content-Disposition'),
      fallbackFilename,
    )
    onProgress?.({ loadedBytes: blob.size })
    triggerDownload(blob, filename)
    return { filename, bytes: blob.size }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    if (!value) continue
    chunks.push(value)
    loadedBytes += value.byteLength
    onProgress?.({ loadedBytes, totalBytes })
  }

  const filename = parseFilename(
    response.headers.get('Content-Disposition'),
    fallbackFilename,
  )
  const blob = new Blob(chunks as BlobPart[], {
    type: response.headers.get('Content-Type') || 'text/csv;charset=utf-8',
  })
  triggerDownload(blob, filename)
  return { filename, bytes: loadedBytes }
}

function triggerDownload(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 立刻回收会让部分浏览器取消下载，延后释放
  setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000)
}

/** 导出进度文案用的人类可读体积 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 设备历史上报导出；时间用毫秒时间戳，与列表查询一致 */
export function exportRecords(params: {
  deviceId?: string
  group?: string
  tag?: string[]
  start?: number
  end?: number
}, onProgress?: (progress: ExportProgress) => void): Promise<ExportResult> {
  return downloadExport('/api/exports/records', params, 'records.csv', onProgress)
}

/** 告警导出；级别/状态/时间范围与告警列表同一套参数 */
export function exportAlerts(params: {
  deviceId?: string
  group?: string
  tag?: string[]
  level?: string
  status?: string
  start?: number
  end?: number
}, onProgress?: (progress: ExportProgress) => void): Promise<ExportResult> {
  return downloadExport('/api/exports/alerts', params, 'alerts.csv', onProgress)
}

export { ExportError }
