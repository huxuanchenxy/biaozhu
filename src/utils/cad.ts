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
import { uploadOverwriteObject } from '@/api'

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
  /** 前端标注类型：'rect' | 'polygon'（缺省时按 points 是否有值判断） */
  type?: string
  /** 所属工具栏（splitedAnnotation / splitedAnnotationPolygon / pictutreAnnotation） */
  toolbarKey?: string
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
    const x = Number(pts[i])
    const y = Number(pts[i + 1])
    if (Number.isFinite(x) && Number.isFinite(y)) out.push({ x, y })
  }
  return out
}

/**
 * 是否为多边形标注。
 * 实测多边形框同时带有 x/y/width/height（其外接矩形）与 points，
 * 因此以 points（≥3 个顶点）或 type==='polygon' / toolbarKey 为准，优先 points。
 */
export function isPolygonBox(box: CadBox): boolean {
  if (polygonCorners(box).length >= 3) return true
  return box.type === 'polygon' || box.toolbarKey === 'splitedAnnotationPolygon'
}

/** 统一取标注的角点：多边形取顶点列表，矩形按 rotation 展开四角 */
export function boxCorners(box: CadBox): CadCorner[] {
  return isPolygonBox(box) ? polygonCorners(box) : rectCorners(box)
}

/** 标注顶点数（多边形=顶点个数，矩形=4） */
export function boxVertexCount(box: CadBox): number {
  return boxCorners(box).length
}

/**
 * 标注中心点（多边形取顶点面积形心，退化时取外接矩形中心；矩形取几何中心）。
 * 用于在叠加层里放置标签文字。
 */
export function boxCenter(box: CadBox): CadCorner {
  const pts = boxCorners(box)
  if (!pts.length) return { x: 0, y: 0 }
  let area = 0
  let cx = 0
  let cy = 0
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]
    const b = pts[(i + 1) % pts.length]
    const cross = a.x * b.y - b.x * a.y
    area += cross
    cx += (a.x + b.x) * cross
    cy += (a.y + b.y) * cross
  }
  area /= 2
  if (Math.abs(area) > 1e-6) return { x: cx / (6 * area), y: cy / (6 * area) }
  // 共线/自交退化：用外接矩形中心
  const xs = pts.map((p) => p.x)
  const ys = pts.map((p) => p.y)
  return {
    x: (Math.min(...xs) + Math.max(...xs)) / 2,
    y: (Math.min(...ys) + Math.max(...ys)) / 2,
  }
}

/** 多边形外接尺寸（像素），用于在信息栏里给出区域大小 */
export function boxExtent(box: CadBox): { w: number; h: number } | null {
  const pts = boxCorners(box)
  if (pts.length < 2) return null
  const xs = pts.map((p) => p.x)
  const ys = pts.map((p) => p.y)
  return {
    w: Math.max(...xs) - Math.min(...xs),
    h: Math.max(...ys) - Math.min(...ys),
  }
}

/** 标注类别中文名：多边形/矩形（用于命名前缀与徽标） */
export function boxShapeLabel(box: CadBox): string {
  return isPolygonBox(box) ? '多边形' : '矩形'
}

/** 射线法判断点是否落在多角点围成的区域内 */
export function pointInCorners(x: number, y: number, corners: CadCorner[]): boolean {
  if (corners.length < 3) return false
  let inside = false
  for (let i = 0, j = corners.length - 1; i < corners.length; j = i++) {
    const xi = corners[i].x
    const yi = corners[i].y
    const xj = corners[j].x
    const yj = corners[j].y
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

/** 标注覆盖面积（平方像素）：多边形用鞋带公式，共线退化时用外接宽高 */
export function boxArea(box: CadBox): number {
  const pts = boxCorners(box)
  if (pts.length < 3) return 0
  let sum = 0
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    sum += pts[j].x * pts[i].y - pts[i].x * pts[j].y
  }
  const area = Math.abs(sum) / 2
  if (area > 1e-6) return area
  const ext = boxExtent(box)
  return ext ? ext.w * ext.h : 0
}

/**
 * 命中选取：返回覆盖给定点（图像自然像素坐标）的标注。
 * 重叠时取面积最小者：多边形（剖面区域）往往覆盖整片图，不先选小框会挡住嵌在里面的尺寸框。
 */
export function boxAtPoint(boxes: CadBox[], x: number, y: number): CadBox | null {
  let hit: CadBox | null = null
  let best = Infinity
  for (const box of boxes) {
    if (!pointInCorners(x, y, boxCorners(box))) continue
    const area = boxArea(box)
    if (area < best) {
      best = area
      hit = box
    }
  }
  return hit
}

/**
 * 权威「类型」清单：与标注工具的「类型」单选项一致，顺序照搬其面板。
 * 数据里 content.type.value 存的是英文代码，这里 code→中文名。
 * 注：前 21 项为尺寸/公差/符号类，后 15 项为 GD&T（几何公差）。
 *     其中螺纹/粗糙度/焊接编号/探伤/气泡/接管等代码在现有样本里未出现过，
 *     按现有命名习惯拟定；若原图数据用了不同代码串，回显会退回原代码，需据实修正。
 */
const TYPE_OPTIONS: Array<{ code: string; label: string }> = [
  { code: 'general-linear-dimension', label: '普通线性尺寸' },
  { code: 'linear-dimension-tolerance', label: '线性尺寸公差' },
  { code: 'radius-dimension', label: '半径尺寸' },
  { code: 'radius-tolerance', label: '半径尺寸公差' },
  { code: 'diameter-dimension', label: '直径尺寸' },
  { code: 'diameter-tolerance', label: '直径尺寸纯公差' },
  { code: 'angular-dimension', label: '角度尺寸' },
  { code: 'angular-tolerance', label: '角度公差' },
  { code: 'chamfer-dimension', label: '倒角尺寸' },
  { code: 'chamfer-tolerance', label: '倒角尺寸公差' },
  { code: 'thread-spec', label: '螺纹规格标注' },
  { code: 'thread-tolerance-param', label: '螺纹规格公差及参数' },
  { code: 'surface-finish-symbol', label: '表面粗糙度符号及参数' },
  { code: 'welding-symbol', label: '焊接符号本体' },
  { code: 'datum-symbol', label: '基准代号/基准符号' },
  { code: 'datum-target', label: '基准目标' },
  { code: 'weld-part-number', label: '焊接/部位编号' },
  { code: 'weld-inspection-ndt', label: '焊接检验/探伤说明' },
  { code: 'view-section-identification', label: '视图/剖视标识' },
  { code: 'balloon-number', label: '圆圈/气泡编号' },
  { code: 'technical-requirements-block', label: '技术要求/说明块' },
  { code: 'straightness', label: '直线度' },
  { code: 'flatness', label: '平面度' },
  { code: 'circularity', label: '圆度' },
  { code: 'cylindricity', label: '圆柱度' },
  { code: 'parallelism', label: '平行度' },
  { code: 'perpendicularity', label: '垂直度' },
  { code: 'angularity', label: '倾斜度' },
  { code: 'position', label: '位置度' },
  { code: 'concentricity', label: '同轴度/同心度' },
  { code: 'symmetry', label: '对称度' },
  { code: 'circular-runout', label: '圆跳动' },
  { code: 'total-runout', label: '全跳动' },
  { code: 'profile-of-a-line', label: '线轮廓度' },
  { code: 'profile-of-a-surface', label: '面轮廓度' },
  { code: 'nozzle', label: '接管' },
]

/**
 * 仅用于回显的补充类型：多边形剖面线/填充区等（不在「类型」单选面板内，
 * 但历史数据里会出现），以及旧代码 runout。保证这些值不会在界面上退成英文代码。
 */
const TYPE_LABEL_EXTRA: Record<string, string> = {
  'single-direction-diagonal-hatch-area': '单向斜线剖面区',
  'double-direction-diagonal-hatch-area': '双向斜线剖面区',
  'cross-hatch-area': '交叉剖面区',
  'dot-fill-texture-area': '点状填充区',
  'honeycomb-fill-texture-area': '蜂窝填充区',
  'line-fill-texture-area': '线条填充区',
  'gradient-fill-texture-area': '渐变填充区',
  runout: '跳动',
}

/** 类型代码 → 中文标签（权威清单 + 回显补充）；未命中回退原代码 */
const TYPE_LABEL: Record<string, string> = {
  ...Object.fromEntries(TYPE_OPTIONS.map((o) => [o.code, o.label])),
  ...TYPE_LABEL_EXTRA,
}

/** 把类型代码转成中文标签；未知代码原样返回 */
export function typeLabel(code: string): string {
  if (!code) return ''
  return TYPE_LABEL[code] || code
}

/** 编辑弹框「类型」单选框的可选项：即权威清单，顺序与标注工具一致 */
export const CAD_TYPE_OPTIONS: Array<{ code: string; label: string }> = TYPE_OPTIONS.map((o) => ({
  ...o,
}))

/**
 * 写回 annotations.json（覆盖 MinIO 上的原对象）。
 * 编辑/删除后把内存里的整份 CadAnnotations 原样序列化上传。
 */
export async function saveCadAnnotations(annotationsKey: string, data: CadAnnotations): Promise<void> {
  await uploadOverwriteObject(fullObjectPath(annotationsKey), data)
}

/** 把 content 归一为「字段数组」（兼容对象映射与数组两种导出形式） */
function normalizeFormFields(content: CadBox['content']): CadFormField[] {
  if (!content) return []
  if (Array.isArray(content)) return content
  return Object.values(content)
}

/** 表单字段标识 → 中文标签（数据自带 name 时优先用 name） */
const FIELD_LABEL: Record<string, string> = {
  type: '类型',
  value: '数值',
  'upper-deviation': '上公差',
  'lower-deviation': '下公差',
  datum: '基准',
  'thread-designation': '螺纹代号',
  'tolerance-grade': '公差等级',
}

/**
 * 把标注的属性表单展开为「中文标签 + 值」列表（只保留有值的项）。
 * 矩形常见类型/数值/上下公差/基准等；多边形通常只有类型。
 */
export function boxFormEntries(box: CadBox): Array<{ label: string; value: string }> {
  const content = box.content
  if (!content) return []
  const raw: Array<[string, CadFormField | undefined]> = Array.isArray(content)
    ? content.map((f, i) => [String(f?.key ?? f?.name ?? i), f] as [string, CadFormField | undefined])
    : Object.entries(content)
  const out: Array<{ label: string; value: string }> = []
  for (const [k, f] of raw) {
    const v = f?.value
    const text =
      v == null ? '' : typeof v === 'number' ? String(v) : typeof v === 'string' ? v.trim() : ''
    if (!text) continue
    const label = f?.name || FIELD_LABEL[k] || k
    out.push({ label, value: k === 'type' ? typeLabel(text) : text })
  }
  return out
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
