/** 扩展 vue-router 的路由元信息类型（独立文件，避免覆盖模块导出） */
import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 是否无需登录即可访问 */
    public?: boolean
    /** 页面标题 */
    title?: string
  }
}
