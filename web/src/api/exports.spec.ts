/**
 * 报表导出 API（v1.30.0）：
 * - 文件名解析兼容 RFC 5987 与普通 filename；
 * - 导出请求带筛选参数、按字节回报进度并触发下载；
 * - 失败时把后端中文 message 透出给界面，不误报成功；
 * - 401 刷新失败时清理会话并给出可读文案。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { refreshMock } = vi.hoisted(() => ({ refreshMock: vi.fn() }))

vi.mock('@/api/http', () => ({
  buildApiUrl: (path: string, params?: Record<string, unknown>) => {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value === undefined) continue
      search.set(key, String(value))
    }
    const query = search.toString()
    return query ? `${path}?${query}` : path
  },
  refreshAccessToken: refreshMock,
}))

import { exportAlerts, exportRecords, formatBytes, parseFilename } from '@/api/exports'
import { getToken, setAuth } from '@/utils/auth'

function csvResponse(body: string, headers: Record<string, string> = {}): Response {
  const bytes = new TextEncoder().encode(body)
  let served = false
  return {
    ok: true,
    status: 200,
    headers: new Headers(headers),
    body: {
      getReader: () => ({
        read: async () => {
          if (served) return { done: true, value: undefined }
          served = true
          return { done: false, value: bytes }
        },
      }),
    },
  } as unknown as Response
}

function errorResponse(message: string, status: number): Response {
  return {
    ok: false,
    status,
    headers: new Headers(),
    json: async () => ({ code: status, message, data: null }),
  } as unknown as Response
}

let downloadClicks: number

beforeEach(() => {
  downloadClicks = 0
  localStorage.clear()
  vi.unstubAllGlobals()
  refreshMock.mockReset()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
    downloadClicks += 1
  })
  Object.defineProperty(URL, 'createObjectURL', {
    value: () => 'blob:mock',
    configurable: true,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    value: () => undefined,
    configurable: true,
  })
})

describe('parseFilename', () => {
  it('优先解析 RFC 5987 编码文件名', () => {
    expect(
      parseFilename(
        "attachment; filename=\"fallback.csv\"; filename*=UTF-8''alerts%202026.csv",
        'x.csv',
      ),
    ).toBe('alerts 2026.csv')
  })

  it('兼容普通 filename 与缺失头', () => {
    expect(
      parseFilename('attachment; filename="alerts_2026-11-01_2026-11-05.csv"', 'x.csv'),
    ).toBe('alerts_2026-11-01_2026-11-05.csv')
    expect(parseFilename(null, 'alerts.csv')).toBe('alerts.csv')
  })
})

describe('导出请求', () => {
  it('带筛选参数、回报进度并使用服务端文件名', async () => {
    setAuth('access-token', { username: 'op', role: 'operator' })
    const progress: number[] = []
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
        csvResponse('\ufeff告警ID\r\nalt-1\r\n', {
          'Content-Disposition': 'attachment; filename="alerts_2026-11-01_2026-11-05.csv"',
          'Content-Type': 'text/csv; charset=utf-8',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await exportAlerts(
      {
        deviceId: 'd-1',
        level: 'critical',
        status: 'open',
        start: 1000,
        end: 2000,
      },
      (p) => progress.push(p.loadedBytes),
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/exports/alerts?')
    expect(String(url)).toContain('deviceId=d-1')
    expect(String(url)).toContain('level=critical')
    expect(String(url)).toContain('start=1000')
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toBe(`Bearer ${getToken()}`)
    expect(result.filename).toBe('alerts_2026-11-01_2026-11-05.csv')
    expect(result.bytes).toBeGreaterThan(0)
    expect(progress[progress.length - 1]).toBe(result.bytes)
    expect(downloadClicks).toBe(1)
  })

  it('422 超限时透出后端提示且不触发下载', async () => {
    setAuth('access-token', { username: 'op', role: 'operator' })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        errorResponse('导出 300000 行超过上限 200000 行，请缩小时间范围，或按设备 / 分组分批导出', 422),
      ),
    )

    await expect(exportRecords({ deviceId: 'd-1' })).rejects.toMatchObject({
      name: 'ExportError',
      status: 422,
      message: expect.stringContaining('超过上限 200000 行'),
    })
    expect(downloadClicks).toBe(0)
  })

  it('401 刷新失败时清理会话并提示重新登录', async () => {
    setAuth('expired', { username: 'op', role: 'operator' }, 'refresh-1')
    refreshMock.mockResolvedValue(false)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => errorResponse('令牌无效或已过期', 401)),
    )

    await expect(exportRecords({ deviceId: 'd-1' })).rejects.toMatchObject({
      message: '登录已过期，请重新登录',
      status: 401,
    })
    expect(getToken()).toBeFalsy()
    expect(downloadClicks).toBe(0)
  })
})

describe('formatBytes', () => {
  it('按 KB / MB 展示导出体积', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})
