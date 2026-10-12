/**
 * CAD 标注页读取与还原封装（前端侧）。
 *
 * 数据都在 MinIO 桶里，前端不持有任何密钥：
 *   - 列目录：GET /cad-api/list?prefix=<桶内前缀>  → server.mjs 用 SigV4 签名调 ListObjectsV2，返回 { objects: string[] }
 *   - 读对象（图片 / annotations.json）：直接走同源桶路径 /<bucket>/<key>
 *     开发由 Vite 代理、生产由 server.mjs 代理，均无签名转发到签名服务再对 MinIO 重新签名。
 *
 * 路径约定（与路由 /cad/<key> 一致，key 即桶内完整对象路径）：
 *   <bucket>/<...任务目录>/annotations/<任务编号>/annotations.json
 * 图片位于同级 <bucket>/<...任务目录>/images/<任务编号>/*.png。
 * 字段与还原规则见仓库根目录《标注JSON还原到大图_字段与规则说明_2026-09.md》。
 */

import { fullObjectPath, normalizeObjectKey } from './minio'

/** annotations.json 里的一个表单控件（content 数组元素），字段按渲染需要宽松取用 */
export interface CadFormField {
  type?: string
  label?: string
  value?: unknown
  /** 部分导出用 key/name 标识控件 */
  key?: string
  name?: string
  uuid?: string
}

/** 单个标注框：矩形/视图分割框用 x/y/width/height/rotation；多边形用 points */
export interface CadBox {
  x?: number
  y?: number
  width?: number
  height?: number
  rotation?: number
  points?: number[]
  /** 形如 pictureList-imgShow-<N>-1，N 即该标注归属的切图序号（从 0 起） */
  displayKey?: string
  /**
   * 标注属性表单。实测为「以字段名为 key 的对象」，每项 { type, name, value }：
   *   content.type  = { type:'radio', name:'类型', value:'<类型代码>' }
   *   content.value = { type:'input', name:'数值', value:'<数值>' }
   * 也兼容早期以数组形式导出的情况。
   */
  content?: CadFormMap | CadFormField[]
  /** 前端渲染名（如“矩形-1”），无业务含义，仅用于展示 */
  name?: string
  color?: string
  uuid?: string
  imageWidth?: number
  imageHeight?: number
}

/** content 表单：对象映射（key=字段标识）或数组 */
export type CadFormMap = Record<string, CadFormField>

/** 标注 json 顶层结构（仅声明还原用到的字段） */
export interface CadAnnotations {
  OSS_KEY?: string
  pdf2img?: string
  splited_image?: Array<{ image?: string } | string>
  pictutreAnnotation?: { content?: CadBox[] }
  splitedAnnotation?: { content?: CadBox[] }
  splitedAnnotationPolygon?: { content?: CadBox[] }
  review?: unknown
}

/** 由路由 key 解析出的目录结构 */
export interface CadPath {
  bucket: string
  /** 任务目录（含桶名，不含结尾斜杠），例：drivdernet_abc/国拨/.../2012.10.25_输送带底部封板02-精剪 */
  taskDir: string
  /** 任务编号目录名，例：1934653195106424210 */
  taskId: string
  /** annotations.json 的完整对象 key */
  annotationsKey: string
  /** 图片目录前缀（含结尾斜杠），用于列目录 */
  imagesPrefix: string
}

/**
 * 解析 /cad/<key> 的 key（桶内完整对象路径）为目录结构。
 * 期望结尾为 `.../annotations/<taskId>/annotations.json`；不匹配时尽量回退：
 * 取倒数第二段为 taskId、去掉末段文件名后的父级的父级为 taskDir。
 */
export function parseCadPath(rawKey: string): CadPath | null {
  const key = normalizeObjectKey(rawKey).replace(/\\/g, '/')
  if (!key) return null
  const segs = key.split('/')
  if (segs.length < 2) return null
  // 末段应为 annotations.json（放宽：任意 .json 也接受）
  const file = segs[segs.length - 1]
  if (!/\.json$/i.test(file)) return null
  const taskId = segs[segs.length - 2]
  const bucket = segs[0]
  // taskDir = 去掉末尾 `annotations/<taskId>/annotations.json` 三段；
  // 若倒数第三段不是 annotations，也按去掉末两段处理（容错命名差异）
  const annIdx = segs.length - 3
  const taskDirSegs = segs.slice(0, annIdx >= 0 && segs[annIdx] === 'annotations' ? annIdx : segs.length - 2)
  const taskDir = taskDirSegs.join('/')
  const annotationsKey = key
  const imagesPrefix = `${taskDir}/images/${taskId}/`
  return { bucket, taskDir, taskId, annotationsKey, imagesPrefix }
}

/** 拼出可读对象的同源代理 URL（经 Vite/server.mjs 转发到 MinIO）：/<bucket>/<key> */
export function cadObjectUrl(key: string): string {
  return fullObjectPath(key)
}

/**
 * 列目录：调 /cad-api/list，返回桶内匹配前缀的对象 key 列表。
 * 传入的 prefix 与项目其它处一致「含桶名」；ListObjectsV2 返回的 Key 相对桶根、不含桶名，
 * 故这里把桶名重新拼回，使返回的 key 与 cadObjectUrl / displayPath 的「含桶名」约定保持一致。
 */
export async function listCadObjects(prefix: string): Promise<string[]> {
  const norm = normalizeObjectKey(prefix)
  const url = `/cad-api/list?prefix=${encodeURIComponent(norm)}`
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`列目录失败 HTTP ${res.status}：${prefix}`)
  }
  const data = await res.json()
  if (data?.error) throw new Error(`${data.error}${data.detail ? `：${data.detail}` : ''}`)
  const raw = Array.isArray(data?.objects) ? (data.objects as string[]) : []
  const bucket = norm.split('/')[0] || ''
  if (!bucket) return raw
  return raw.map((k) => (k.startsWith(`${bucket}/`) ? k : `${bucket}/${k}`))
}

/** 读取并解析 annotations.json */
export async function fetchCadAnnotations(key: string): Promise<CadAnnotations> {
  const res = await fetch(cadObjectUrl(key))
  if (!res.ok) throw new Error(`读取标注失败 HTTP ${res.status}：${key}`)
  return (await res.json()) as CadAnnotations
}

/** 从 displayKey 提取归属切图序号 N（pictureList-imgShow-<N>-1）；无匹配返回 -1 */
export function imageIndexOfBox(box: CadBox): number {
  const m = /-imgShow-(\d+)-/.exec(box.displayKey || '')
  return m ? Number(m[1]) : -1
}

/** 取 splited_image 列表里第 n 个的文件名（只取文件名，路径不可信，见说明文档第 5 条） */
export function splitedImageName(ann: CadAnnotations, n: number): string {
  const item = ann.splited_image?.[n]
  const ref = typeof item === 'string' ? item : item?.image || ''
  return baseName(ref)
}

/** 取对象 key 的文件名部分 */
export function baseName(key: string): string {
  const s = (key || '').replace(/\\/g, '/')
  const i = s.lastIndexOf('/')
  return i >= 0 ? s.slice(i + 1) : s
}

/** 一个标注框换算出的四角点（图像像素坐标，未加视图偏移） */
export interface CadCorner {
  x: number
  y: number
}

/**
 * 按说明文档第 4 步展开矩形角点：绕左上角、图像坐标系（Y 向下）顺时针旋转 rotation 度。
 * rotation 为 0/缺省时退化为轴对齐矩形。
 */
export function rectCorners(box: CadBox): CadCorner[] {
  const x = Number(box.x) || 0
  const y = Number(box.y) || 0
  const w = Number(box.width) || 0
  const h = Number(box.height) || 0
  const deg = Number(box.rotation) || 0
  if (!deg) {
    return [
      { x, y },
      { x: x + w, y },
      { x: x + w, y: y + h },
      { x, y: y + h },
    ]
  }
  const t = (deg * Math.PI) / 180
  const cos = Math.cos(t)
  const sin = Math.sin(t)
  return [
    { x, y },
    { x: x + w * cos, y: y + w * sin },
    { x: x + w * cos - h * sin, y: y + w * sin + h * cos },
    { x: x - h * sin, y: y + h * cos },
  ]
}

/** 多边形角点：points 为扁平数组 [x1,y1,x2,y2,...]，两两一组 */
export function polygonCorners(box: CadBox): CadCorner[] {
  const pts = box.points || []
  const out: CadCorner[] = []
  for (let i = 0; i + 1 < pts.length; i += 2) {
    out.push({ x: Number(pts[i]) || 0, y: Number(pts[i + 1]) || 0 })
  }
  return out
}

/**
 * 类型代码 → 中文标签。实测精剪的类型值为英文代码（如 general-linear-dimension）。
 * 未收录的代码回退显示原始代码（GD&T 类代码若本就不在此表，会在界面上直接显代码）。
 */
const TYPE_LABEL: Record<string, string> = {
  'general-linear-dimension': '线性尺寸',
  'linear-dimension-tolerance': '线性尺寸公差',
  'diameter-dimension': '直径尺寸',
  'radius-dimension': '半径尺寸',
  'angular-dimension': '角度尺寸',
  'chamfer-dimension': '倒角尺寸',
  'technical-requirements-block': '技术要求',
  // 常见 GD&T（几何公差）代码，部分图纸会用到；未命中则回退原代码
  straightness: '直线度',
  flatness: '平面度',
  circularity: '圆度',
  cylindricity: '圆柱度',
  'profile-of-a-line': '线轮廓度',
  'profile-of-a-surface': '面轮廓度',
  symmetry: '对称度',
  runout: '跳动',
  'total-runout': '全跳动',
  'parallelism': '平行度',
  perpendicularity: '垂直度',
  angularity: '倾斜度',
  'position': '位置度',
  'concentricity': '同心度',
}

/** 把类型代码转成中文标签；未知代码原样返回 */
export function typeLabel(code: string): string {
  if (!code) return ''
  return TYPE_LABEL[code] || code
}

/** 把 content 归一为「字段数组」（兼容对象映射与数组两种导出形式） */
function normalizeFormFields(content: CadBox['content']): CadFormField[] {
  if (!content) return []
  if (Array.isArray(content)) return content
  return Object.values(content)
}

/**
 * 从标注 content 里取“类型”与“数值”（已映射为中文）。
 * 优先按固定字段取：类型=content.type.value（或 type 为 radio 的项），数值=content.value.value；
 * 取不到时退化为首个非类型的标量值。
 */
export function boxTypeAndValue(box: CadBox): { type: string; value: string } {
  const map = (box.content && !Array.isArray(box.content) ? box.content : {}) as CadFormMap
  const fields = normalizeFormFields(box.content)
  const scalar = (v: unknown): string =>
    v == null ? '' : typeof v === 'number' ? String(v) : typeof v === 'string' ? v : ''

  // 类型：先看固定 key，再看 type==='radio' 的项
  let type = scalar(map.type?.value)
  if (!type) type = scalar(fields.find((f) => f?.type === 'radio')?.value)

  // 数值：先看固定 key，再看 name 含“数值”的项，最后退为首个非类型且非空的标量
  let value = scalar(map.value?.value)
  if (!value) value = scalar(fields.find((f) => f?.name && f.name.includes('数值'))?.value)
  if (!value) {
    for (const f of fields) {
      const s = scalar(f?.value)
      if (s && f?.type !== 'radio' && f !== map.type) {
        value = s
        break
      }
    }
  }

  return { type: typeLabel(type), value }
}

/** 标注展示名：优先用 json 自带 name，否则用前缀 + 序号 */
export function boxDisplayName(box: CadBox, prefix: string, index: number): string {
  return box.name || `${prefix}-${index}`
}
