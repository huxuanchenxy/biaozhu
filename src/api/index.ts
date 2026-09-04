/**
 * 接口统一管理
 * 约定：一个业务模块一个文件，在 index.ts 中集中导出
 * 路径不用写 /api 前缀，request 已统一加了 baseURL
 */
import http from '@/utils/request'
import type { DocJsonRecord, SessionItem, SkillItem } from './types'

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

/* ------------------------------ 标注数据 ------------------------------ */

/**
 * 拉取标注数据 json 并做合格性校验（能解析成 JSON、顶层是对象数组）。
 * 目前读 public/doc 下的本地文件，后续换后端接口只改这里，视图层不动。
 */
export async function getDocJson(fileName: string): Promise<DocJsonRecord[]> {
  const res = await fetch(`/doc/${fileName}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const text = await res.text()

  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('文件内容不是合法 JSON')
  }
  if (!Array.isArray(parsed)) throw new Error('顶层结构不是数组')
  if (parsed.some((item) => typeof item !== 'object' || item === null || Array.isArray(item))) {
    throw new Error('数组元素存在非对象项')
  }
  return parsed as DocJsonRecord[]
}

/**
 * 保存标注结果：上传当前标签页的整个 json。
 * TODO: 后端接口尚未提供，先模拟成功；接口就绪后改成 http.post(`/doc/${fileName}`, data) 之类
 */
export function saveDocJson(_fileName: string, _data: DocJsonRecord[]): Promise<void> {
  return Promise.resolve()
}
