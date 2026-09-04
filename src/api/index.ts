/**
 * 接口统一管理
 * 约定：一个业务模块一个文件，在 index.ts 中集中导出
 * 路径不用写 /api 前缀，request 已统一加了 baseURL
 */
import http from '@/utils/request'
import type { SessionItem, SkillItem } from './types'

/* ------------------------------ 会话相关 ------------------------------ */

/** 会话列表 */
export const getSessionList = () => http.get<SessionItem[]>('/session/list')

/** 会话详情 */
export const getSessionDetail = (sessionId: string) =>
  http.get<any>(`/session/${sessionId}`)

/* ------------------------------ 技能相关 ------------------------------ */

/** 技能列表 */
export const getSkillList = () => http.get<SkillItem[]>('/skill/list')

/* ------------------------------ 示例接口 ------------------------------ */

/** 示例：GET 带参数 */
export const getDemoList = (params: { page: number; size: number }) =>
  http.get<any[]>('/demo/list', { params })

/** 示例：POST 提交 JSON */
export const createDemo = (data: Record<string, any>) =>
  http.post<any>('/demo/create', data)

/** 示例：文件上传（后端接口 POST /api/file/upload/batch） */
export const uploadFile = (file: File) => http.upload<any>('/file/upload/batch', file)
