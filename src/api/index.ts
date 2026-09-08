/**
 * 接口统一管理
 * 约定：一个业务模块一个文件，在 index.ts 中集中导出
 * 路径不用写 /api 前缀，request 已统一加了 baseURL
 */
import http from '@/utils/request'
import { fullObjectPath, getObjectText } from '@/utils/minio'
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
 * 解析并校验标注 json 文本：能解析成 JSON、顶层是对象数组、元素均为对象。
 * 本地与 MinIO 两种来源共用同一套校验规则。
 */
function parseDocJson(text: string): DocJsonRecord[] {
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
 * 拉取标注数据 json（本地 public/doc）。后续换后端接口只改这里，视图层不动。
 */
export async function getDocJson(fileName: string): Promise<DocJsonRecord[]> {
  const res = await fetch(`/doc/${fileName}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return parseDocJson(await res.text())
}

/**
 * 从 MinIO 按 object key 拉取标注数据 json，校验规则与本地一致。
 */
export async function getMinioDocJson(key: string): Promise<DocJsonRecord[]> {
  return parseDocJson(await getObjectText(key))
}

/**
 * 上传覆盖 MinIO 上的 json 对象（后端 POST /api/file/uploadOverwrite）。
 * multipart/form-data：file=当前编辑的整个 json 文件，fullObjectPath=/<bucket>/<key>。
 * 直连后端（VITE_API_DIRECT_BASE，含 /api），不走 Vite/server.mjs 代理；传绝对 URL 时 axios 会忽略实例 baseURL。
 */
const UPLOAD_API_BASE = (
  import.meta.env.VITE_API_DIRECT_BASE ||
  import.meta.env.VITE_APP_BASE_API ||
  '/api'
).replace(/\/$/, '')

export async function uploadOverwriteJson(fullPath: string, data: DocJsonRecord[]): Promise<void> {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const fileName = fullPath.split('/').pop() || 'data.json'
  const file = new File([blob], fileName, { type: 'application/json' })
  await http.upload<any>(`${UPLOAD_API_BASE}/file/uploadOverwrite`, file, { fullObjectPath: fullPath })
}

/**
 * 保存标注结果：上传当前标签页的整个 json。
 * MinIO 模式（传 minioKey）调 uploadOverwrite 覆盖写回原对象；
 * 本地回退模式（无 minioKey）暂无写接口，模拟成功。
 */
export async function saveDocJson(_fileName: string, data: DocJsonRecord[], minioKey?: string): Promise<void> {
  if (minioKey) {
    await uploadOverwriteJson(fullObjectPath(minioKey), data)
    return
  }
  return Promise.resolve()
}
