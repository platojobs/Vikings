/**
 * 测试环境补丁：Node 26 自带实验性 `localStorage`（未配置 `--localstorage-file`
 * 时为 undefined），会遮蔽 jsdom 的实现并让 `window.localStorage` 也取到 undefined。
 * 这里在缺失时用 jsdom 自带的 Storage 回填，保证测试与生产代码使用同一套语义；
 * Node 20 等环境中 jsdom 已正常注入，此补丁不会生效。
 */
import { JSDOM } from 'jsdom'

if (!globalThis.localStorage) {
  const dom = new JSDOM('', { url: 'http://localhost:5173' })
  Object.defineProperty(globalThis, 'localStorage', {
    value: dom.window.localStorage,
    configurable: true,
  })
}
