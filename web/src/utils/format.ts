/** 时间与数值格式化工具 */

const pad = (value: number): string => String(value).padStart(2, '0')

/** 格式化为本地时区 `YYYY-MM-DD HH:mm:ss`，空值显示占位符 */
export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** 格式化为 `HH:mm:ss`，用于图表时间轴 */
export function formatShortTime(value: number): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 把 ISO 字符串 / 毫秒时间戳统一转换为毫秒时间戳 */
export function toTimestamp(value: string | number | null | undefined): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (!value) return Date.now()
  const parsed = Date.parse(String(value))
  return Number.isNaN(parsed) ? Date.now() : parsed
}

/** 传感器读数格式化，保留指定小数位 */
export function formatMetric(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return Number(value).toFixed(digits)
}
