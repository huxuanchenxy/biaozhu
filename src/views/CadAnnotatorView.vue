<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, toRaw, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  baseName,
  boxAtPoint,
  boxCenter,
  boxCorners,
  boxDisplayName,
  boxExtent,
  boxFormEntries,
  boxShapeLabel,
  boxVertexCount,
  cadObjectUrl,
  CAD_RECT_TYPE_OPTIONS,
  CAD_POLYGON_TYPE_OPTIONS,
  CAD_RECT_ATTR_FIELDS,
  fetchCadAnnotations,
  imageIndexOfBox,
  isPolygonBox,
  listCadObjects,
  parseCadPath,
  saveCadAnnotations,
  splitedImageName,
  type CadAnnotations,
  type CadBox,
  type CadCorner,
  type CadFormMap,
  type CadPath,
} from '@/utils/cad'
import EditIcon from '@/icons/EditIcon.vue'
import DeleteIcon from '@/icons/DeleteIcon.vue'

/**
 * CAD 图纸标注页：/cad/<bucket>/<任务目录>/annotations/<任务编号>/annotations.json
 * 左栏列出该任务 images/<任务编号>/ 下的全部 png 切图，并把属于它的标注框画在图上；
 * 右栏罗列当前选中图片的标注信息（类型 / 数值）。字段与还原规则见根目录说明文档。
 */

const route = useRoute()

/** 当前路由携带的桶内对象 key（:key(.*) 一般为字符串，兼容数组情况） */
const rawKey = computed(() => {
  const p = route.params.key
  const key = Array.isArray(p) ? p.join('/') : String(p ?? '')
  return key.trim()
})

/** 解析出的目录结构；不合法为 null */
const cadPath = computed<CadPath | null>(() => parseCadPath(rawKey.value))

const loading = ref(false)
const loadError = ref('')
const ann = ref<CadAnnotations | null>(null)

/** 一张切图：对象 key、展示路径、归属它的标注框 */
interface CadImage {
  key: string
  /** 去掉桶名的相对展示路径 */
  displayPath: string
  name: string
  boxes: CadBox[]
}

const images = ref<CadImage[]>([])
const selectedIndex = ref(0)

/** 每张图的真实像素尺寸（@load 后写入），用于 SVG viewBox 换算 */
const dims = reactive<Record<string, { w: number; h: number }>>({})

/**
 * 悬停坐标：记录当前悬停图片、光标在视口(.img-stage)内的显示像素偏移，以及换算后的图片真实像素坐标。
 * left/top 用于定位跟随光标的标签；natX/natY 为标注 JSON 同系的像素坐标（十字线也用它）。
 */
const hover = reactive({ key: '', show: false, left: 0, top: 0, natX: 0, natY: 0 })

/**
 * 每张图独立的变换与交互态：
 *  - transforms[key] = { scale, x, y }：作用于 .img-wrap 的 translate+scale（transform-origin 0 0）。
 *  - panMode[key]：是否开启「抓手/拖动」模式。开启后拖动平移、滚轮失效；关闭时滚轮缩放、不响应拖动。
 */
interface CadTransform {
  scale: number
  x: number
  y: number
}
const transforms = reactive<Record<string, CadTransform>>({})
const panMode = reactive<Record<string, boolean>>({})
const MIN_SCALE = 0.1
const MAX_SCALE = 8

/** 每张图的默认适配态（contain：整张图完整可见并居中），加载完成/双击复位时回到这个态 */
const fits = reactive<Record<string, CadTransform>>({})

function tf(key: string): CadTransform {
  if (!transforms[key]) transforms[key] = { scale: 1, x: 0, y: 0 }
  return transforms[key]
}

/**
 * 计算并应用 contain 适配：图片按宽度铺满时若高度超出视口，则整体缩小到刚好全显并居中；
 * 未超出则保持 1×，仅垂直居中。wrap 布局原点在视口左上角（transform-origin 0 0）。
 */
function applyFit(key: string, stage: HTMLElement) {
  const d = dims[key]
  if (!d || !d.w || !d.h) return
  const sw = stage.clientWidth
  const sh = stage.clientHeight
  if (!sw || !sh) return
  const dispH = (d.h / d.w) * sw
  const s = Math.min(1, sh / dispH)
  const fit = { scale: s, x: (sw - sw * s) / 2, y: (sh - dispH * s) / 2 }
  fits[key] = fit
  transforms[key] = { ...fit }
}
function wrapStyle(key: string) {
  const t = transforms[key]
  return t ? { transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})` } : {}
}

/* ---- 视图平移动画：把视口外的选中框缓动到视口中央（不改变缩放） ---- */
const panAnims = new Map<string, number>()
function cancelPan(key: string) {
  const id = panAnims.get(key)
  if (id != null) {
    cancelAnimationFrame(id)
    panAnims.delete(key)
  }
}
function panTo(key: string, x: number, y: number, animate = true) {
  cancelPan(key)
  const t = tf(key)
  if (!animate) {
    t.x = x
    t.y = y
    return
  }
  const sx = t.x
  const sy = t.y
  const dur = 280
  const t0 = performance.now()
  const step = (now: number) => {
    const k = Math.min(1, (now - t0) / dur)
    const e = 1 - Math.pow(1 - k, 3) // easeOutCubic
    t.x = sx + (x - sx) * e
    t.y = sy + (y - sy) * e
    if (k < 1) panAnims.set(key, requestAnimationFrame(step))
    else panAnims.delete(key)
  }
  panAnims.set(key, requestAnimationFrame(step))
}

/** 右栏选中某框时：若它在当前视口内不完整可见，则保持缩放把它的中心缓动平移到视口中央 */
function revealBoxInView(img: CadImage, box: CadBox) {
  const i = images.value.indexOf(img)
  if (i < 0) return
  const stage = document.getElementById(`cad-img-${i}`)?.querySelector('.img-stage') as HTMLElement | null
  const imgEl = stage?.querySelector('.img-el') as HTMLImageElement | null
  const d = dims[img.key]
  if (!stage || !imgEl || !d || !d.w || !d.h) return
  const sr = stage.getBoundingClientRect()
  const ir = imgEl.getBoundingClientRect()
  if (!ir.width || !ir.height) return
  const toScreenX = (x: number) => ir.left + (x / d.w) * ir.width
  const toScreenY = (y: number) => ir.top + (y / d.h) * ir.height
  const cs = boxCorners(box)
  if (!cs.length) return
  const minX = Math.min(...cs.map((c) => toScreenX(c.x)))
  const maxX = Math.max(...cs.map((c) => toScreenX(c.x)))
  const minY = Math.min(...cs.map((c) => toScreenY(c.y)))
  const maxY = Math.max(...cs.map((c) => toScreenY(c.y)))
  const m = 4
  if (minX >= sr.left - m && maxX <= sr.right + m && minY >= sr.top - m && maxY <= sr.bottom + m) return
  const t = tf(img.key)
  const c = boxCenter(box)
  panTo(
    img.key,
    t.x + (sr.left + sr.width / 2 - toScreenX(c.x)),
    t.y + (sr.top + sr.height / 2 - toScreenY(c.y)),
  )
}

/** 拖动中的临时状态（非响应式，仅记录起点）；moved 用于区分「拖动后松手」与「单击」 */
let drag: { key: string; sx: number; sy: number; ox: number; oy: number; moved: boolean } | null = null
const dragging = ref(false)

function togglePan(key: string) {
  panMode[key] = !panMode[key]
  // 退出抓手模式时清掉可能残留的拖动
  if (!panMode[key]) {
    drag = null
    dragging.value = false
  }
}

/** 滚轮缩放（拖动中也可用）：以光标位置为锚点缩放，保持光标下的图像点不动 */
function onWheel(img: CadImage, e: WheelEvent) {
  // 先拦截：无论是否已到缩放上下限，都不让滚轮冒泡影响外层滚动条
  e.preventDefault()
  cancelPan(img.key) // 用户手动缩放时中断自动平移，避免两套位置互相覆盖
  const stage = e.currentTarget as HTMLElement
  const rect = stage.getBoundingClientRect()
  const cx = e.clientX - rect.left
  const cy = e.clientY - rect.top
  const t = tf(img.key)
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, t.scale * factor))
  if (next === t.scale) return
  const k = next / t.scale
  t.x = cx - k * (cx - t.x)
  t.y = cy - k * (cy - t.y)
  t.scale = next
}

/** 抓手模式：按下开始平移；绘制模式下改走框选/顶点采集 */
function onStageMouseDown(img: CadImage, e: MouseEvent) {
  if (drawMode[img.key] === 'rect') {
    const pt = naturalPoint(img, e)
    if (pt) {
      draft.imageIndex = images.value.indexOf(img)
      draft.shape = 'rect'
      draft.x0 = pt.x
      draft.y0 = pt.y
      draft.x1 = pt.x
      draft.y1 = pt.y
      rectDrawing = true
      e.preventDefault()
    }
    return
  }
  if (drawMode[img.key] === 'polygon') return // 多边形顶点在 click 里累加
  // 几何编辑模式：先命中当前选中框的手柄→缩放；否则命中框体→平移（均按自然坐标算）
  if (editMode[img.key]) {
    const pt = naturalPoint(img, e)
    if (!pt) return
    const ab = activeBox.value
    if (ab && img.boxes.includes(ab)) {
      const vi = vertexIndexAt(img, ab, pt)
      if (vi >= 0) {
        startVertexDrag(ab, img, vi, e, pt)
        return
      }
    }
    const hit = boxAtPoint(
      boxesForImage(img).map((b) => b.box),
      pt.x,
      pt.y,
    )
    if (hit) startShapeDrag(hit, img, e, pt)
    return
  }
  if (!panMode[img.key]) return
  cancelPan(img.key) // 手动平移时中断自动平移动画
  const t = tf(img.key)
  drag = { key: img.key, sx: e.clientX, sy: e.clientY, ox: t.x, oy: t.y, moved: false }
  dragging.value = true
  e.preventDefault()
}
function onWindowMouseMove(e: MouseEvent) {
  if (geomDrag) {
    geomDragMove(e)
    return
  }
  if (rectDrawing && draft.shape === 'rect') {
    const pt = naturalPointAt(draft.imageIndex, e.clientX, e.clientY)
    if (pt) {
      draft.x1 = pt.x
      draft.y1 = pt.y
    }
    return
  }
  if (!drag) return
  const t = tf(drag.key)
  const dx = e.clientX - drag.sx
  const dy = e.clientY - drag.sy
  if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true
  t.x = drag.ox + dx
  t.y = drag.oy + dy
}
function onWindowMouseUp() {
  if (geomDrag) {
    geomDrag = null
    geomDragging.value = false
    return
  }
  if (rectDrawing) {
    rectDrawing = false
    suppressNextClick = true
    finalizeRect()
    return
  }
  drag = null
  dragging.value = false
}

/** 双击复位到默认适配态（整张图全显） */
function resetTransform(img: CadImage) {
  const f = fits[img.key]
  transforms[img.key] = f ? { ...f } : { scale: 1, x: 0, y: 0 }
}

/** 每张图的光标样式：拖动中→grabbing；抓手模式→grab；编辑模式下命中框→move；否则→默认（不显放大镜） */
function cursorFor(key: string): string {
  if (drawMode[key]) return 'crosshair'
  if (geomDragging.value) return 'grabbing'
  if (panMode[key]) return dragging.value && drag && drag.key === key ? 'grabbing' : 'grab'
  if (editMode[key]) return hover.show && hover.key === key && hoverBox.value ? 'move' : 'default'
  return hover.show && hover.key === key && hoverBox.value ? 'pointer' : 'default'
}

/** 叠加层尺寸基准：viewBox 为自然像素，按图宽归一（以 1000px 为 1 单）使线宽/字体不随图大小差异 */
function unit(key: string): number {
  return (dims[key]?.w || 1000) / 1000
}
function vertexRadius(key: string): number {
  return 5 * unit(key)
}

/** 框配色：与截图一致的绿色系为主，多类型时循环取色 */
const PALETTE = ['#22c55e', '#3b82f6', '#f59e0b', '#a855f7', '#ef4444', '#14b8a6', '#eab308']
function boxColor(i: number): string {
  return PALETTE[i % PALETTE.length]
}

/** 叠加层开关：显示标注 / 多边形半透明填充 / 高亮框顶点手柄 */
const showBoxes = ref(true)
const fillPolygons = ref(true)
const showVertices = ref(true)

/** 形状筛选：全部 / 仅矩形 / 仅多边形 */
type ShapeFilter = 'all' | 'rect' | 'polygon'
const shapeFilter = ref<ShapeFilter>('all')

/**
 * 右栏：平铺显示全部标注（跨图片）。
 * 每行记录归属图片的下标与框在该图内的序号，配色/命名与左栏叠加层保持一致；
 * 点击某行 → 选中并滚动到对应图片、高亮对应框；反之点图上的框也会定位到该行。
 */
const allRows = computed(() => {
  const rows: Array<{
    box: CadBox
    imageIndex: number
    index: number
    color: string
    name: string
    imageName: string
    isPolygon: boolean
    /** 属性表单里全部有值字段（含类型/数值），空值字段不展示；数值项带 isValue 供高亮 */
    fields: Array<{ label: string; value: string; isValue: boolean }>
    /** 顶点数 */
    vertices: number
    /** 区域外接尺寸文本（主要给多边形用） */
    extent: string
  }> = []
  images.value.forEach((img, imageIndex) => {
    img.boxes.forEach((box, i) => {
      const isPolygon = isPolygonBox(box)
      const entries = boxFormEntries(box)
      const ext = boxExtent(box)
      // 动态字段：接口 content 里有哪些键就展示哪些，值为空的键值对整体不显示；
      // 固定「类型」在前、「数值」其次，其余按 content 原始顺序
      const fields: Array<{ label: string; value: string; isValue: boolean }> = []
      const typeEntry = entries.find((e) => e.label === '类型')
      if (typeEntry) fields.push({ ...typeEntry, isValue: false })
      const valueEntry = entries.find((e) => e.label === '数值')
      if (valueEntry) fields.push({ ...valueEntry, isValue: true })
      for (const e of entries) {
        if (e.label !== '类型' && e.label !== '数值') fields.push({ ...e, isValue: false })
      }
      rows.push({
        box,
        imageIndex,
        index: i,
        color: boxColor(i),
        name: boxDisplayName(box, boxShapeLabel(box), i + 1),
        imageName: img.name,
        isPolygon,
        fields,
        vertices: boxVertexCount(box),
        extent: ext ? `${Math.round(ext.w)}×${Math.round(ext.h)} px` : '',
      })
    })
  })
  return rows
})

/** 从名称末尾解析序号（矩形-N / 多边形-N）；无尾数字时排到末尾 */
function shapeSeqOfName(name: string): number {
  const m = /(\d+)\s*$/.exec((name || '').trim())
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER
}

/** 按筛选条件后的行（按矩形/多边形分组排列），以及两种形状下的总数 */
const visibleRows = computed(() => {
  const bySeq = (a: (typeof allRows.value)[number], b: (typeof allRows.value)[number]) =>
    shapeSeqOfName(a.name) - shapeSeqOfName(b.name)
  // 组内先按图片顺序（images 保持 list 接口返回的原始顺序），同图内再按名称序号；并按组内位置重新编号 no
  const byImgThenSeq = (a: (typeof allRows.value)[number], b: (typeof allRows.value)[number]) =>
    a.imageIndex - b.imageIndex || bySeq(a, b)
  const rectRows = allRows.value
    .filter((r) => !r.isPolygon)
    .sort(byImgThenSeq)
    .map((r, i) => ({ ...r, no: i + 1 }))
  const polyRows = allRows.value
    .filter((r) => r.isPolygon)
    .sort(byImgThenSeq)
    .map((r, i) => ({ ...r, no: i + 1 }))
  if (shapeFilter.value === 'rect') return rectRows
  if (shapeFilter.value === 'polygon') return polyRows
  // 全部：先矩形后多边形，使右栏按形状分类展示
  return [...rectRows, ...polyRows]
})
const rectCount = computed(() => allRows.value.filter((r) => !r.isPolygon).length)
const polyCount = computed(() => allRows.value.filter((r) => r.isPolygon).length)

/** 把切图展示路径拆为「/images/ 之前的公共前缀」与「/images/.../名称 短路径」 */
function splitCadPath(dp: string): { prefix: string; short: string } {
  const idx = dp.lastIndexOf('/images/')
  if (idx === -1) {
    // 无 /images/ 段：退化为只显示文件名
    const slash = dp.lastIndexOf('/')
    return { prefix: slash >= 0 ? dp.slice(0, slash) : '', short: slash >= 0 ? dp.slice(slash) : dp }
  }
  return { prefix: dp.slice(0, idx), short: dp.slice(idx) }
}
/** 卡片头只显示短路径（/images/.../名称.png） */
function shortDisplayPath(img: CadImage): string {
  return splitCadPath(img.displayPath).short
}
/** 工具条显示的公共路径前缀（取第一张图 /images/ 之前的部分） */
const imagesPathPrefix = computed(() => (images.value[0] ? splitCadPath(images.value[0].displayPath).prefix : ''))

/** 是否处于单图模式（路由 key 直指某张切图） */
const isSingleImage = computed(() => !!cadPath.value?.focusImageName)

/**
 * 某张切图的独立标注页 URL：/cad/<图片对象完整路径>（每段单独编码，保留 '/'）。
 * 新标签打开即为“只针对当前图片”的标注页，目录列表缩为这一张。
 */
function singleImageUrl(img: CadImage): string {
  const encoded = img.key.split('/').map(encodeURIComponent).join('/')
  return `${location.origin}${location.pathname}#/cad/${encoded}`
}

/** 高亮某条标注（右栏点击 → 选中所属图片 + 高亮对应框 + 滚动定位） */
const activeBox = ref<CadBox | null>(null)
/** 当前悬停命中的标注（图上悬停预览 / 右栏行悬停反向高亮共用） */
const hoverBox = ref<CadBox | null>(null)
/** 本次选中的来源：闪烁只提示用户视线不在的那一侧（点图闪列表行，点列表闪图上的框） */
const flashSource = ref<'list' | 'img' | ''>('')
function activateRow(row: (typeof allRows.value)[number]) {
  selectedIndex.value = row.imageIndex
  activeBox.value = row.box
}
function focusRow(row: (typeof allRows.value)[number]) {
  const same = activeBox.value === row.box
  activateRow(row)
  activeBox.value = same ? null : row.box
  if (!same) {
    flashSource.value = 'list'
    nextTick(() => {
      document.getElementById(`cad-img-${row.imageIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      // 选中框在当前视口外时保持缩放平移过去，使其可见
      const img = images.value[row.imageIndex]
      if (img) revealBoxInView(img, row.box)
    })
  }
}

/** 把高亮框在右栏对应行滚到可见 */
function scrollToRow(box: CadBox) {
  const row = allRows.value.find((r) => r.box === box)
  if (!row) return
  nextTick(() => {
    document
      .getElementById(`ann-row-${row.imageIndex}-${row.index}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

/** 切换高亮框（再次点同一个则取消） */
function toggleActiveBox(box: CadBox) {
  activeBox.value = activeBox.value === box ? null : box
  flashSource.value = activeBox.value ? 'img' : ''
  if (activeBox.value) scrollToRow(activeBox.value)
}

/* ------------------------------------------------------------------
 * 标注信息：修改 / 删除（写回 MinIO 上的 annotations.json）
 *  - 行右侧「编辑」→ 弹框：类型用单选框，其余文本字段可编辑；多边形通常只有类型。
 *  - 行右侧「删除」→ 二次确认后从源数据与各图 boxes 中移除。
 *  - box 对象与 ann.value 里的源数组共享引用，就地改 content 即同步反映到写回体。
 * ------------------------------------------------------------------ */
type AnnRow = (typeof allRows.value)[number]

const saving = ref(false)
const editDialog = ref(false)
const editBox = ref<CadBox | null>(null)
const editType = ref('')
/** 当前编辑的框是否自带「类型」字段（无类型的框不展示类型单选） */
const editHasType = ref(false)
/** 当前编辑的框是否为多边形（决定类型单选项取哪套清单） */
const editIsPolygon = ref(false)
/** 除类型外的可编辑文本字段（由 box.content 动态得出） */
const editFields = ref<Array<{ key: string; label: string; value: string }>>([])

/** 类型选项：按形状选用矩形/多边形清单 + 当前值（若未收录则追加一项，避免选中项不显示） */
const typeOptions = computed(() => {
  const opts = [...(editIsPolygon.value ? CAD_POLYGON_TYPE_OPTIONS : CAD_RECT_TYPE_OPTIONS)]
  const cur = editType.value
  if (cur && !opts.some((o) => o.code === cur)) opts.unshift({ code: cur, label: `${cur}（未收录）` })
  return opts
})

function startEdit(row: AnnRow) {
  const box = row.box
  editBox.value = box
  editType.value = ''
  editHasType.value = false
  editIsPolygon.value = isPolygonBox(box)
  editFields.value = []
  const content = box.content
  if (content && !Array.isArray(content)) {
    for (const [k, f] of Object.entries(content)) {
      const v = f?.value
      const text = v == null ? '' : typeof v === 'number' ? String(v) : typeof v === 'string' ? v : ''
      if (k === 'type' || f?.type === 'radio') {
        editType.value = text
        editHasType.value = true
      } else {
        editFields.value.push({ key: k, label: f?.name || k, value: text })
      }
    }
  }
  editDialog.value = true
}

async function submitEdit() {
  const box = editBox.value
  const content = box?.content
  if (box && content && !Array.isArray(content)) {
    const map = content as CadFormMap
    // 回写类型（radio）
    for (const [k, f] of Object.entries(map)) {
      if ((k === 'type' || f?.type === 'radio') && f) f.value = editType.value
    }
    // 回写其余文本字段
    for (const ef of editFields.value) {
      const f = map[ef.key]
      if (f) f.value = ef.value
    }
  }
  editDialog.value = false
  await persist('已保存修改')
}

async function deleteBox(row: AnnRow) {
  try {
    await ElMessageBox.confirm(
      `确定删除标注「${row.name}」？此操作会写回并覆盖原 annotations.json，不可恢复。`,
      '删除标注',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  const src = ann.value
  if (!src) return
  const removeFrom = (arr?: CadBox[]): boolean => {
    if (!arr) return false
    let i = arr.indexOf(row.box)
    if (i < 0 && row.box.uuid) i = arr.findIndex((b) => b.uuid === row.box.uuid)
    if (i >= 0) {
      arr.splice(i, 1)
      return true
    }
    return false
  }
  const ok = removeFrom(src.splitedAnnotation?.content) || removeFrom(src.splitedAnnotationPolygon?.content)
  // 同步 UI：从各图 boxes 中移除
  for (const img of images.value) {
    const j = img.boxes.indexOf(row.box)
    if (j >= 0) img.boxes.splice(j, 1)
  }
  if (activeBox.value === row.box) activeBox.value = null
  if (hoverBox.value === row.box) hoverBox.value = null
  if (!ok) {
    ElMessage.warning('未在源数据中找到该标注，未写回')
    return
  }
  await persist('已删除标注')
}

/** 把当前内存里的整份 annotations 写回 MinIO；返回是否保存成功 */
async function persist(okMsg: string): Promise<boolean> {
  const p = cadPath.value
  if (!p || !ann.value) return false
  saving.value = true
  try {
    await saveCadAnnotations(p.annotationsKey, ann.value)
    ElMessage.success(okMsg)
    return true
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message ?? '未知错误'}`)
    return false
  } finally {
    saving.value = false
  }
}

/* ------------------------------------------------------------------
 * 新增标注：在切图上绘制几何（矩形拖拽框选 / 多边形单击顶点），
 *  完成后弹属性框选类型(+数值)，写入 ann.value 源数组并覆盖回 MinIO。
 * ------------------------------------------------------------------ */
type DrawShape = 'rect' | 'polygon'
const drawMode = reactive<Record<string, DrawShape | ''>>({})
const draft = reactive<{
  imageIndex: number
  shape: DrawShape | ''
  x0: number
  y0: number
  x1: number
  y1: number
  points: number[]
}>({ imageIndex: -1, shape: '', x0: 0, y0: 0, x1: 0, y1: 0, points: [] })
const addDialog = ref(false)
const addIsPolygon = ref(false)
const addType = ref('')
/** 矩形新增时除类型外的属性值（key 同 CAD_RECT_ATTR_FIELDS，留空不写入） */
const addAttrs = ref<Record<string, string>>({})
const pendingBox = ref<{ box: CadBox; imageIndex: number } | null>(null)
let rectDrawing = false
let suppressNextClick = false

/** 新增弹框类型选项：按形状选用矩形/多边形清单 */
const addTypeOptions = computed(() => [...(addIsPolygon.value ? CAD_POLYGON_TYPE_OPTIONS : CAD_RECT_TYPE_OPTIONS)])

/** 按图片下标 + 视口坐标换算自然像素坐标（窗口级监听里用，currentTarget 非 stage） */
function naturalPointAt(i: number, clientX: number, clientY: number): CadCorner | null {
  const img = images.value[i]
  if (!img) return null
  const stage = document.getElementById(`cad-img-${i}`)?.querySelector('.img-stage') as HTMLElement | null
  const imgEl = stage?.querySelector('.img-el') as HTMLImageElement | null
  const d = dims[img.key]
  if (!imgEl || !d) return null
  const r = imgEl.getBoundingClientRect()
  if (!r.width || !r.height) return null
  return { x: ((clientX - r.left) / r.width) * d.w, y: ((clientY - r.top) / r.height) * d.h }
}

function toggleDraw(img: CadImage, shape: DrawShape) {
  const key = img.key
  const i = images.value.indexOf(img)
  if (drawMode[key] === shape) {
    stopDraw()
    return
  }
  // 绘制与几何编辑互斥：开始绘制前静默放弃未保存的几何修改并退出编辑
  if (editMode[key]) {
    restoreGeometry()
    geomSnapshot = null
    editDirty.value = false
    editMode[key] = false
    activeBox.value = null
  }
  stopDraw()
  drawMode[key] = shape
  panMode[key] = false
  selectedIndex.value = i
  draft.imageIndex = i
  draft.shape = shape
  draft.points = []
  draft.x0 = draft.y0 = draft.x1 = draft.y1 = 0
  activeBox.value = null
}
function stopDraw() {
  for (const k of Object.keys(drawMode)) drawMode[k] = ''
  draft.shape = ''
  draft.imageIndex = -1
  draft.points = []
  rectDrawing = false
}

/* ------------------------------------------------------------------
 * 几何编辑：「进入编辑」后可拖动标注框整体平移，拖动顶点改变大小
 *  （矩形四角缩放，多边形逐顶点变形）；「保存编辑」写回 MinIO，
 *  取消/Esc 用进入时的快照回滚。
 * ------------------------------------------------------------------ */
const editMode = reactive<Record<string, boolean>>({})
/** 进入编辑时的几何快照：按框对象引用记录几何字段，回滚时就地还原（不换引用，不依赖 uuid） */
interface GeomSnap {
  x?: number
  y?: number
  width?: number
  height?: number
  rotation?: number
  points?: number[]
}
let geomSnapshot: Map<CadBox, GeomSnap> | null = null
const editDirty = ref(false)

/** 收集全部标注（精剪矩形 + 多边形，含未归属的）的几何快照 */
function snapshotGeometry(): Map<CadBox, GeomSnap> {
  const m = new Map<CadBox, GeomSnap>()
  const pool: CadBox[] = [
    ...(toRaw(ann.value)?.splitedAnnotation?.content ?? []),
    ...(toRaw(ann.value)?.splitedAnnotationPolygon?.content ?? []),
  ]
  for (const b of pool) {
    m.set(toRaw(b) as CadBox, {
      x: b.x,
      y: b.y,
      width: b.width,
      height: b.height,
      rotation: b.rotation,
      points: b.points ? [...b.points] : undefined,
    })
  }
  return m
}

/** 按快照就地还原几何：img.boxes 与 ann 源数组共享同一框引用，两边同时恢复 */
function restoreGeometry() {
  if (!geomSnapshot) return
  for (const [b, s] of geomSnapshot) {
    b.x = s.x
    b.y = s.y
    b.width = s.width
    b.height = s.height
    b.rotation = s.rotation
    if (s.points && b.points) b.points.splice(0, b.points.length, ...s.points)
    else b.points = s.points ? [...s.points] : s.points
  }
}

/** 进入/退出某图的几何编辑：全局互斥，一次只编辑一张图 */
function toggleEdit(img: CadImage) {
  const key = img.key
  if (editMode[key]) {
    cancelEdit()
    return
  }
  stopDraw()
  for (const k of Object.keys(editMode)) editMode[k] = false
  for (const k of Object.keys(panMode)) panMode[k] = false
  geomSnapshot = ann.value ? snapshotGeometry() : null
  editDirty.value = false
  editMode[key] = true
  selectedIndex.value = images.value.indexOf(img)
  activeBox.value = null
}

/** 保存：把修改后的整份 annotations 写回 MinIO，成功后退出编辑 */
async function saveEdit() {
  if (!editDirty.value) {
    exitEdit()
    return
  }
  const ok = await persist('已保存编辑')
  if (ok) exitEdit()
}
// 模板里「✎ 进入编辑」按钮组当前被注释隐藏，这里保留函数引用以免 noUnusedLocals 报错；恢复按钮时可删除下一行
void [toggleEdit, saveEdit]

/** 取消：有未保存修改时二次确认，确认后就地还原几何快照 */
async function cancelEdit() {
  if (editDirty.value) {
    try {
      await ElMessageBox.confirm('有未保存的几何修改，放弃并退出编辑？', '退出编辑', {
        type: 'warning',
        confirmButtonText: '放弃修改',
        cancelButtonText: '继续编辑',
      })
    } catch {
      return
    }
  }
  restoreGeometry()
  activeBox.value = null
  hoverBox.value = null
  exitEdit()
}

function exitEdit() {
  for (const k of Object.keys(editMode)) editMode[k] = false
  geomSnapshot = null
  editDirty.value = false
  geomDrag = null
  geomDragging.value = false
}

/* ---- 拖动/缩放交互 ---- */
type GeomCorners = Array<{ x: number; y: number }>
let geomDrag: {
  key: string
  box: CadBox
  mode: 'move' | 'resize'
  vi: number
  sx: number
  sy: number
  ox: number
  oy: number
  ow: number
  oh: number
  orig: GeomCorners
  moved: boolean
} | null = null
const geomDragging = ref(false)

/** 某角点/顶点在自然坐标下的命中索引（手柄抓取半径内），未命中返回 -1 */
function vertexIndexAt(img: CadImage, box: CadBox, pt: CadCorner): number {
  const r = vertexRadius(img.key) * 3.4
  const cs = cornersOf(box)
  for (let i = 0; i < cs.length; i++) {
    const dx = pt.x - cs[i].x
    const dy = pt.y - cs[i].y
    if (dx * dx + dy * dy <= r * r) return i
  }
  return -1
}

/** 按下框体：整体平移（pt 为阶段层已算好的自然坐标） */
function startShapeDrag(box: CadBox, img: CadImage, e: MouseEvent, pt: CadCorner) {
  if (!editMode[img.key]) return
  e.preventDefault()
  suppressNextClick = true
  activeBox.value = box
  geomDrag = {
    key: img.key,
    box,
    mode: 'move',
    vi: -1,
    sx: pt.x,
    sy: pt.y,
    ox: Number(box.x) || 0,
    oy: Number(box.y) || 0,
    ow: Number(box.width) || 0,
    oh: Number(box.height) || 0,
    orig: cornersOf(box).map((c) => ({ ...c })),
    moved: false,
  }
  geomDragging.value = true
}

/** 按下顶点手柄：矩形四角缩放 / 多边形单顶点变形 */
function startVertexDrag(box: CadBox, img: CadImage, vi: number, e: MouseEvent, pt: CadCorner) {
  if (!editMode[img.key] || activeBox.value !== box) return
  e.preventDefault()
  suppressNextClick = true
  geomDrag = {
    key: img.key,
    box,
    mode: 'resize',
    vi,
    sx: pt.x,
    sy: pt.y,
    ox: Number(box.x) || 0,
    oy: Number(box.y) || 0,
    ow: Number(box.width) || 0,
    oh: Number(box.height) || 0,
    orig: cornersOf(box).map((c) => ({ ...c })),
    moved: false,
  }
  geomDragging.value = true
}

function geomDragMove(e: MouseEvent) {
  const g = geomDrag
  if (!g) return
  const i = images.value.findIndex((im) => im.key === g.key)
  const pt = naturalPointAt(i, e.clientX, e.clientY)
  if (!pt) return
  const dx = pt.x - g.sx
  const dy = pt.y - g.sy
  const box = g.box
  const rotated = !isPolygonBox(box) && Number(box.rotation) || 0
  // 带 rotation 的矩形仅支持平移，手柄拖拽（resize）不生效
  if (g.mode === 'resize' && rotated) return
  if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
    g.moved = true
    editDirty.value = true
  }
  if (g.mode === 'move') {
    box.x = g.ox + dx
    box.y = g.oy + dy
    if (isPolygonBox(box)) {
      const pts = box.points ?? []
      for (let k = 0; k + 1 < pts.length; k += 2) {
        pts[k] = g.orig[k / 2].x + dx
        pts[k + 1] = g.orig[k / 2].y + dy
      }
    }
  } else if (isPolygonBox(box)) {
    const pts = box.points ?? []
    pts[g.vi * 2] = pt.x
    pts[g.vi * 2 + 1] = pt.y
    const xs = pts.filter((_, k) => k % 2 === 0)
    const ys = pts.filter((_, k) => k % 2 === 1)
    box.x = Math.min(...xs)
    box.y = Math.min(...ys)
    box.width = Math.max(...xs) - box.x
    box.height = Math.max(...ys) - box.y
  } else if (g.orig.length === 4) {
    // 轴对齐矩形：对锚角点不动，拖动当前角点缩放
    const fixed = g.orig[(g.vi + 2) % 4]
    box.x = Math.min(fixed.x, pt.x)
    box.y = Math.min(fixed.y, pt.y)
    box.width = Math.max(1, Math.abs(pt.x - fixed.x))
    box.height = Math.max(1, Math.abs(pt.y - fixed.y))
  }
}

function onStageDblClick(img: CadImage, e: MouseEvent) {
  if (drawMode[img.key] === 'polygon') {
    e.preventDefault()
    finalizePolygon()
    return
  }
  resetTransform(img)
}

function draftRect() {
  const x = Math.min(draft.x0, draft.x1)
  const y = Math.min(draft.y0, draft.y1)
  return { x, y, w: Math.abs(draft.x1 - draft.x0), h: Math.abs(draft.y1 - draft.y0) }
}
function draftPolyPoints(): string {
  let s = ''
  for (let k = 0; k + 1 < draft.points.length; k += 2) s += `${draft.points[k]},${draft.points[k + 1]} `
  return s.trim()
}
function draftVertices(): CadCorner[] {
  const out: CadCorner[] = []
  for (let k = 0; k + 1 < draft.points.length; k += 2) out.push({ x: draft.points[k], y: draft.points[k + 1] })
  return out
}
/** 最后一个已确定的顶点（橡皮筋起点） */
function lastDraftVertex(): CadCorner {
  const vs = draftVertices()
  return vs.length ? vs[vs.length - 1] : { x: 0, y: 0 }
}
/** 自然坐标下是否落在“起点大空心圆”上（≥ 3 顶点时才作为闭合目标） */
function nearFirstVertex(x: number, y: number, key: string): boolean {
  if (draft.points.length < 6) return false
  const dx = x - draft.points[0]
  const dy = y - draft.points[1]
  const r = vertexRadius(key) * 2.6
  return dx * dx + dy * dy <= r * r
}
/** 当前光标是否悬停在起点上（提示“点击闭合”） */
function polygonClosingHint(key: string): boolean {
  return (
    draft.shape === 'polygon' &&
    hover.show &&
    hover.key === key &&
    nearFirstVertex(hover.natX, hover.natY, key)
  )
}

function finalizeRect() {
  const r = draftRect()
  if (r.w < 4 || r.h < 4) {
    ElMessage.info('框太小，已取消')
    stopDraw()
    return
  }
  openAddDialog('rect', { x: r.x, y: r.y, width: r.w, height: r.h })
}
function finalizePolygon() {
  if (draft.points.length / 2 < 3) {
    ElMessage.info('至少需要 3 个顶点')
    return
  }
  openAddDialog('polygon', { points: [...draft.points] })
}

/** 反查某张切图在 splited_image 里的序号 N（用于生成 displayKey） */
function splitIndexForImage(img: CadImage, fallback: number): number {
  const data = ann.value
  if (data) {
    const arr = data.splited_image ?? []
    for (let n = 0; n < arr.length; n++) if (splitedImageName(data, n) === img.name) return n
  }
  return fallback
}
function genUuid(): string {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (c?.randomUUID) return c.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function openAddDialog(
  shape: DrawShape,
  geom: { x?: number; y?: number; width?: number; height?: number; points?: number[] },
) {
  const i = draft.imageIndex
  const img = images.value[i]
  if (!img) {
    stopDraw()
    return
  }
  const d = dims[img.key]
  const n = splitIndexForImage(img, i)
  const box: CadBox = {
    type: shape === 'polygon' ? 'polygon' : 'rect',
    toolbarKey: shape === 'polygon' ? 'splitedAnnotationPolygon' : 'splitedAnnotation',
    displayKey: `pictureList-imgShow-${n}-1`,
    rotation: 0,
    imageWidth: d?.w,
    imageHeight: d?.h,
    uuid: genUuid(),
    name: '',
  }
  if (shape === 'polygon' && geom.points) {
    const pts = geom.points
    box.points = pts
    const xs = pts.filter((_, k) => k % 2 === 0)
    const ys = pts.filter((_, k) => k % 2 === 1)
    box.x = Math.min(...xs)
    box.y = Math.min(...ys)
    box.width = Math.max(...xs) - box.x
    box.height = Math.max(...ys) - box.y
  } else if (geom.x != null) {
    box.x = geom.x
    box.y = geom.y
    box.width = geom.width
    box.height = geom.height
  }
  pendingBox.value = { box, imageIndex: i }
  addIsPolygon.value = shape === 'polygon'
  addType.value = ''
  addAttrs.value = {}
  stopDraw()
  addDialog.value = true
}

function cancelAdd() {
  addDialog.value = false
  pendingBox.value = null
}

/** 同类形状（矩形/多边形）下一个序号：扫描当前已显示名称里的最大数字 +1 */
function nextShapeSeq(isPolygon: boolean): number {
  const prefix = isPolygon ? '多边形' : '矩形'
  const re = new RegExp(`^${prefix}-(\\d+)$`)
  let max = 0
  for (const r of allRows.value) {
    if (r.isPolygon !== isPolygon) continue
    const m = re.exec((r.name || '').trim())
    if (m) max = Math.max(max, Number(m[1]))
  }
  return max + 1
}

async function submitAdd() {
  const pb = pendingBox.value
  const src = ann.value
  if (!pb || !src) return
  const { box, imageIndex } = pb
  const content: CadFormMap = { type: { type: 'radio', name: '类型', value: addType.value } }
  if (!addIsPolygon.value) {
    // 矩形：按需写入非空属性（数值/上下公差/基准/螺纹代号/公差等级），留空字段不落盘
    for (const f of CAD_RECT_ATTR_FIELDS) {
      const v = (addAttrs.value[f.key] || '').trim()
      if (v) content[f.key] = { type: 'input', name: f.label, value: v }
    }
  }
  box.content = content
  // 名称由系统生成：取同类形状已显示名称里的最大序号 +1（矩形-N / 多边形-N 各自独立）
  box.name = `${addIsPolygon.value ? '多边形' : '矩形'}-${nextShapeSeq(addIsPolygon.value)}`
  if (addIsPolygon.value) {
    if (!src.splitedAnnotationPolygon) src.splitedAnnotationPolygon = { content: [] }
    if (!src.splitedAnnotationPolygon.content) src.splitedAnnotationPolygon.content = []
    src.splitedAnnotationPolygon.content.push(box)
  } else {
    if (!src.splitedAnnotation) src.splitedAnnotation = { content: [] }
    if (!src.splitedAnnotation.content) src.splitedAnnotation.content = []
    src.splitedAnnotation.content.push(box)
  }
  if (images.value[imageIndex]) images.value[imageIndex].boxes.push(box)
  addDialog.value = false
  pendingBox.value = null
  activeBox.value = box
  scrollToRow(box)
  await persist('已新增标注')
}

/**
 * 舞台层单击：按光标的自然像素坐标做命中选取，而不是依赖 SVG 元素自身的可点区域——
 * 矩形框 fill=none 时 SVG 不会把内部当命中区，叠在上面的大剖面多边形也会抢走点击。
 * 重叠时取面积最小者，保证嵌在剖面区里的小尺寸框能优先点中；拖动后的松手不算点击。
 */
function onStageClick(img: CadImage, e: MouseEvent) {
  selectedIndex.value = images.value.indexOf(img)
  if (suppressNextClick) {
    suppressNextClick = false
    return
  }
  if (drawMode[img.key] === 'polygon') {
    const pt = naturalPoint(img, e)
    if (!pt) return
    // 已有 ≥ 3 个顶点时，点回第一个顶点→闭合并结束
    if (nearFirstVertex(pt.x, pt.y, img.key)) {
      finalizePolygon()
      return
    }
    draft.points.push(Math.round(pt.x), Math.round(pt.y))
    return
  }
  if (drawMode[img.key] === 'rect') return
  if (drag?.moved) return
  const pt = naturalPoint(img, e)
  if (!pt) {
    activeBox.value = null
    return
  }
  const hit = boxAtPoint(
    boxesForImage(img).map((b) => b.box),
    pt.x,
    pt.y,
  )
  if (!hit) {
    activeBox.value = null
    return
  }
  toggleActiveBox(hit)
}

/** 一张图上某个框的多角点（像素坐标） */
function cornersOf(box: CadBox) {
  return boxCorners(box)
}
function pointsAttr(box: CadBox): string {
  return cornersOf(box)
    .map((c) => `${c.x},${c.y}`)
    .join(' ')
}
/** 按筛选条件决定这张图上需要叠加绘制的框（保留原图内序号，使配色与右栏一致） */
function boxesForImage(img: CadImage): Array<{ box: CadBox; bi: number }> {
  if (!showBoxes.value) return []
  return img.boxes
    .map((box, bi) => ({ box, bi }))
    .filter(({ box }) => shapeFilter.value === 'all' || (shapeFilter.value === 'polygon') === isPolygonBox(box))
}

/** 是否处于强调态（选中或悬停命中） */
function isEmphasized(box: CadBox): boolean {
  return activeBox.value === box || hoverBox.value === box
}
function shapeFill(box: CadBox, bi: number): string {
  if (isEmphasized(box)) return boxColor(bi)
  return isPolygonBox(box) && fillPolygons.value ? boxColor(bi) : 'none'
}
function shapeFillOpacity(box: CadBox): number {
  if (activeBox.value === box) return 0.3
  if (hoverBox.value === box) return 0.22
  return isPolygonBox(box) && fillPolygons.value ? 0.14 : 0
}
function shapeStrokeWidth(box: CadBox): number {
  if (activeBox.value === box) return 9
  return hoverBox.value === box ? 7 : 5
}
function shapeOpacity(box: CadBox): number {
  return activeBox.value && activeBox.value !== box && hoverBox.value !== box ? 0.4 : 1
}

/** 图片加载完成：记录真实像素尺寸，并应用整张可见的默认适配 */
function onImgLoad(key: string, e: Event) {
  const img = e.target as HTMLImageElement
  dims[key] = { w: img.naturalWidth, h: img.naturalHeight }
  const stage = img.closest('.img-stage') as HTMLElement | null
  if (stage) applyFit(key, stage)
}

/**
 * 光标位置换算为图片自然像素坐标（与标注 JSON 同坐标系）；超出图片时为负值/越界值。
 * 以 .img-el 的实时矩形为基准，因此缩放/平移后依旧准确。
 */
function naturalPoint(img: CadImage, e: MouseEvent): CadCorner | null {
  const stage = e.currentTarget as HTMLElement
  const imgEl = stage.querySelector('.img-el') as HTMLImageElement | null
  const d = dims[img.key]
  if (!imgEl || !d) return null
  const r = imgEl.getBoundingClientRect()
  if (!r.width || !r.height) return null
  return {
    x: ((e.clientX - r.left) / r.width) * d.w,
    y: ((e.clientY - r.top) / r.height) * d.h,
  }
}

/**
 * 光标在视口内移动（含图片以外的灰色区域）：换算出图片自然像素坐标，
 * 并实时算出命中的标注（仅用于悬停预览，拖动中不干预）。
 */
function onStageMove(img: CadImage, e: MouseEvent) {
  const stage = e.currentTarget as HTMLElement
  const stageRect = stage.getBoundingClientRect()
  hover.key = img.key
  hover.left = e.clientX - stageRect.left
  hover.top = e.clientY - stageRect.top
  const pt = naturalPoint(img, e)
  if (pt) {
    hover.natX = Math.round(pt.x)
    hover.natY = Math.round(pt.y)
    hoverBox.value = dragging.value || geomDragging.value ? null : boxAtPoint(boxesForImage(img).map((b) => b.box), pt.x, pt.y)
  } else {
    hover.natX = 0
    hover.natY = 0
    hoverBox.value = null
  }
  hover.show = true
}

function onImgLeave() {
  hover.show = false
  hoverBox.value = null
}

function selectImage(i: number) {
  selectedIndex.value = i
  activeBox.value = null
}

/** 一张图上的多边形个数（卡片头信息用） */
function polyCountOf(img: CadImage): number {
  return img.boxes.filter(isPolygonBox).length
}
/** 一张图上的矩形个数（卡片头信息用） */
function rectCountOf(img: CadImage): number {
  return img.boxes.filter((b) => !isPolygonBox(b)).length
}

/** 选中的框在右栏滚动定位用（简单高亮即可，暂不做跨栏联动滚动） */
const imageListRef = ref<HTMLElement | null>(null)

/** 未能按 displayKey 匹配到切图的标注数量（会被暂挂在第一张图上） */
const unassignedCount = ref(0)

/**
 * 载入：解析路径 → 读 annotations.json → 列 images/<taskId> 目录 →
 * 按 displayKey 的切图序号把每个框归到对应图片。
 */
async function load() {
  activeBox.value = null
  hoverBox.value = null
  images.value = []
  ann.value = null
  selectedIndex.value = 0
  loadError.value = ''
  unassignedCount.value = 0
  // 换图时清空上一份的缩放/平移/抓手态与尺寸缓存
  for (const k of Object.keys(transforms)) delete transforms[k]
  for (const k of Object.keys(fits)) delete fits[k]
  for (const k of Object.keys(panMode)) delete panMode[k]
  for (const k of Object.keys(dims)) delete dims[k]
  drag = null
  dragging.value = false
  geomDrag = null
  geomDragging.value = false
  hover.show = false
  const path = cadPath.value
  if (!path) {
    loadError.value =
      '路径不合法，应为 /cad/<桶>/<任务目录>/annotations/<任务编号>/annotations.json，或单图模式 /cad/<桶>/<任务目录>/images/<任务编号>/<图片名>.png'
    return
  }
  loading.value = true
  try {
    const [data, objectKeys] = await Promise.all([
      fetchCadAnnotations(path.annotationsKey),
      listCadObjects(path.imagesPrefix),
    ])
    ann.value = data

    // 只保留 png，保持接口（ListObjectsV2）返回的原始顺序，不再按文件名重排
    const pngKeys = objectKeys.filter((k) => /\.png$/i.test(k))

    // 文件名 → 对象 key，供按 splited_image 归属匹配
    const byName = new Map<string, string>()
    for (const k of pngKeys) byName.set(baseName(k), k)

    // 收集所有待还原框（精剪矩形 + 多边形）
    const allBoxes: CadBox[] = [
      ...(data.splitedAnnotation?.content ?? []),
      ...(data.splitedAnnotationPolygon?.content ?? []),
    ]

    // 归组：key = 图片对象 key
    const grouped = new Map<string, CadBox[]>()
    const unassigned: CadBox[] = []
    for (const box of allBoxes) {
      const n = imageIndexOfBox(box)
      let imgKey: string | undefined
      if (n >= 0) {
        const name = splitedImageName(data, n)
        imgKey = byName.get(name) ?? pngKeys[n]
      }
      if (!imgKey && pngKeys.length === 1) imgKey = pngKeys[0]
      if (!imgKey) {
        unassigned.push(box)
        continue
      }
      if (!grouped.has(imgKey)) grouped.set(imgKey, [])
      grouped.get(imgKey)!.push(box)
    }

    const bucketPrefix = `${path.bucket}/`
    images.value = pngKeys.map((k) => ({
      key: k,
      displayPath: k.startsWith(bucketPrefix) ? k.slice(bucketPrefix.length) : k,
      name: baseName(k),
      boxes: grouped.get(k) ?? [],
    }))

    // 单图模式：路由 key 直接指向某张切图时，仅保留该图（功能与目录模式一致，只是范围缩到当前图片）
    if (path.focusImageName) {
      const only = images.value.filter((im) => im.name === path.focusImageName)
      if (only.length) {
        images.value = only
      } else {
        // 该文件在 images 目录下不存在：清空列表，只给“无此文件”错误（不再回退展示全部）
        images.value = []
        loadError.value = `未找到图片：${path.focusImageName}（目录 ${path.imagesPrefix} 下不存在该文件）`
        return
      }
    }

    // 有框但没匹配到图的，挂到第一张图上，避免丢框
    if (unassigned.length && images.value.length) {
      images.value[0].boxes.push(...unassigned)
      unassignedCount.value = unassigned.length
    }

    if (!images.value.length) {
      ElMessage.warning('未在 images 目录找到 png 切图')
    }
  } catch (e: any) {
    const msg = e?.message ?? '未知错误'
    // 单图模式：标注文件路径是由 URL 里的“任务编号”反推的，编号/目录写错时读不到
    // （MinIO 对不存在对象返回 403/404）。给“找不到该图片或其标注文件”的友好提示，
    // 不直接把内部反推的 annotations.json 路径抛给用户。
    if (path.focusImageName && /HTTP\s+(403|404)/i.test(msg)) {
      loadError.value = `找不到该图片或其标注文件：${path.focusImageName}（请检查图片路径中的任务编号「${path.taskId}」与文件名是否正确）`
    } else {
      loadError.value = `加载失败：${msg}`
    }
  } finally {
    loading.value = false
  }
}

/** 键盘全局：Esc 取消退出绘制；Delete 对选中框弹出删除确认 */
function onWindowKeyDown(e: KeyboardEvent) {
  // Esc：绘制中（矩形/多边形）取消退出；弹框打开时交给弹框自身处理
  if (e.key === 'Escape') {
    if (editDialog.value || addDialog.value) return
    // 编辑模式：Esc 退出（有未保存修改时弹确认）
    if (Object.values(editMode).some(Boolean)) {
      cancelEdit()
      return
    }
    const drawing = draft.shape !== '' || Object.values(drawMode).some((m) => m !== '')
    if (drawing) {
      e.preventDefault()
      stopDraw()
      ElMessage.info('已取消绘制')
    }
    return
  }
  if (e.key !== 'Delete') return
  // 弹框打开中、或焦点在输入控件里时不拦截
  if (editDialog.value || addDialog.value) return
  const t = e.target as HTMLElement | null
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
  const box = activeBox.value
  if (!box) return
  const row = allRows.value.find((r) => r.box === box)
  if (!row) return
  e.preventDefault()
  deleteBox(row)
}

onMounted(() => {
  load()
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
  window.addEventListener('keydown', onWindowKeyDown)
})
onBeforeUnmount(() => {
  for (const k of panAnims.keys()) cancelPan(k)
  window.removeEventListener('mousemove', onWindowMouseMove)
  window.removeEventListener('mouseup', onWindowMouseUp)
  window.removeEventListener('keydown', onWindowKeyDown)
})
watch(rawKey, load)
</script>

<template>
  <div class="cad-page">
    <!-- 左：切图列表 + 标注框叠加 -->
    <section class="pane pane-left">
      <header class="pane-head">
        <span class="pane-head-l">
          <span class="pane-title">小图片切割</span>
          <span v-if="imagesPathPrefix" class="toolbar-path" :title="imagesPathPrefix">{{ imagesPathPrefix }}</span>
        </span>
        <span v-if="loading" class="hint">加载中…</span>
        <span v-else class="hint">{{ images.length }} 张切图</span>
      </header>
      <!-- <div class="cad-path">当前图片：{{ currentImage?.displayPath || (cadPath?.imagesPrefix ?? '') }}</div> -->

      <!-- 叠加层工具条：形状筛选 + 绘制开关 -->
      <div class="cad-toolbar">
        <!-- <el-radio-group v-model="shapeFilter" size="small">
          <el-radio-button value="all">全部 {{ allRows.length }}</el-radio-button>
          <el-radio-button value="rect">矩形 {{ rectCount }}</el-radio-button>
          <el-radio-button value="polygon">多边形 {{ polyCount }}</el-radio-button>
        </el-radio-group>
        <el-checkbox v-model="showBoxes" size="small">标注叠加</el-checkbox>
        <el-checkbox v-model="fillPolygons" size="small">多边形填充</el-checkbox>
        <el-checkbox v-model="showVertices" size="small">顶点手柄</el-checkbox> -->
        <span class="toolbar-hint">实线=矩形・虚线=多边形・单击框可联动右栏・「进入编辑」可拖动/改大小</span>
      </div>

      <div ref="imageListRef" v-loading="loading" class="pane-body img-list">
        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" />
        <div v-else-if="!loading && !images.length" class="empty">暂无图片</div>

        <article
          v-for="(img, i) in images"
          :id="`cad-img-${i}`"
          :key="img.key"
          :class="['img-card', { 'img-card--active': i === selectedIndex }]"
          @click="selectImage(i)"
        >
          <div class="img-card-head">
            <span class="img-path" :title="img.displayPath">{{ shortDisplayPath(img) }}</span>
            <a
              v-if="!isSingleImage"
              class="img-single-link"
              :href="singleImageUrl(img)"
              target="_blank"
              rel="noopener"
              title="在新标签打开仅针对当前图片的独立标注页"
              @click.stop
            >
              单图页
            </a>
            <span class="img-meta">
              {{ img.boxes.length }} 个标注
              <em v-if="rectCountOf(img)">· 矩形 {{ rectCountOf(img) }}</em>
              <em v-if="polyCountOf(img)">· 多边形 {{ polyCountOf(img) }}</em>
            </span>
            <el-button
              size="small"
              round
              :disabled="!!drawMode[img.key]"
              :type="panMode[img.key] ? 'primary' : 'default'"
              @click.stop="togglePan(img.key)"
            >
              {{ panMode[img.key] ? '👉 拖动中' : '✋ 抓手' }}
            </el-button>
            <span class="add-group">
              <span class="add-group-label">＋ 新增</span>
              <el-button
                size="small"
                round
                title="在本图新增一个矩形标注"
                :type="drawMode[img.key] === 'rect' ? 'primary' : 'default'"
                @click.stop="toggleDraw(img, 'rect')"
              >
                ▭ 矩形
              </el-button>
              <el-button
                size="small"
                round
                title="在本图新增一个多边形标注"
                :type="drawMode[img.key] === 'polygon' ? 'primary' : 'default'"
                @click.stop="toggleDraw(img, 'polygon')"
              >
                ◇ 多边形
              </el-button>
            </span>
            <span class="add-group edit-group">
              <span class="add-group-label">✎ 编辑</span>
              <el-button
                size="small"
                round
                :disabled="!!drawMode[img.key]"
                :loading="editMode[img.key] && saving"
                :type="editMode[img.key] ? 'primary' : 'default'"
                :title="editMode[img.key] ? '保存本次几何修改并写回 MinIO' : '进入编辑：可拖动标注框、拖角点改大小'"
                @click.stop="editMode[img.key] ? saveEdit() : toggleEdit(img)"
              >
                {{ editMode[img.key] ? '💾 保存编辑' : '✎ 进入编辑' }}
              </el-button>
              <el-button v-if="editMode[img.key]" size="small" round @click.stop="cancelEdit">取消</el-button>
            </span>
          </div>
          <div v-if="editMode[img.key]" class="draw-bar edit-bar">
            <span class="draw-tip">
              编辑模式：拖动框体平移；单击选中后拖动手柄可改变矩形大小 / 多边形顶点（Esc 或「取消」放弃修改）
            </span>
          </div>
          <div v-if="drawMode[img.key]" class="draw-bar">
            <span class="draw-tip">
              {{
                drawMode[img.key] === 'rect'
                  ? '绘制矩形：在图上按住鼠标拖拽框选范围（Esc 取消）'
                  : '绘制多边形：单击依次添加顶点，点回起点的空心圆即闭合（也可双击或点「完成」，Esc 取消）'
              }}
            </span>
            <el-button
              v-if="drawMode[img.key] === 'polygon'"
              size="small"
              type="primary"
              :disabled="draftVertices().length < 3"
              @click.stop="finalizePolygon"
              >完成</el-button
            >
            <el-button size="small" @click.stop="stopDraw">取消</el-button>
          </div>
          <div class="img-card-body">
            <div
              class="img-stage"
              :style="{ cursor: cursorFor(img.key) }"
              @wheel="onWheel(img, $event)"
              @mousedown="onStageMouseDown(img, $event)"
              @click.stop="onStageClick(img, $event)"
              @dblclick="onStageDblClick(img, $event)"
              @mousemove="onStageMove(img, $event)"
              @mouseleave="onImgLeave"
            >
              <div class="img-wrap" :style="wrapStyle(img.key)">
                <img
                  :src="cadObjectUrl(img.key)"
                  :alt="img.name"
                  class="img-el"
                  draggable="false"
                  @load="onImgLoad(img.key, $event)"
                />
                <svg
                  v-if="dims[img.key]"
                  class="img-overlay"
                  :viewBox="`0 0 ${dims[img.key].w} ${dims[img.key].h}`"
                  preserveAspectRatio="none"
                >
                  <g
                    v-for="{ box, bi } in boxesForImage(img)"
                    :key="bi"
                    :opacity="shapeOpacity(box)"
                  >
                    <polygon
                      class="box-shape"
                      :class="{
                        'box-shape--poly': isPolygonBox(box),
                        'box-shape--active': activeBox === box,
                        'box-shape--flash': activeBox === box && flashSource === 'list',
                        'box-shape--hover': hoverBox === box && activeBox !== box,
                      }"
                      :points="pointsAttr(box)"
                      :fill="shapeFill(box, bi)"
                      :fill-opacity="shapeFillOpacity(box)"
                      :stroke="boxColor(bi)"
                      :stroke-dasharray="isPolygonBox(box) ? '8 5' : undefined"
                      :stroke-width="shapeStrokeWidth(box)"
                      vector-effect="non-scaling-stroke"
                      stroke-linejoin="round"
                    />
                    <!-- 选中框：顶点手柄（编辑模式下常显且可拖拽改大小） -->
                    <template v-if="activeBox === box">
                      <circle
                        v-if="showVertices || editMode[img.key]"
                        v-for="(c, ci) in cornersOf(box)"
                        :key="`v-${ci}`"
                        :class="['vertex-handle', { 'vertex-handle--drag': editMode[img.key] }]"
                        :cx="c.x"
                        :cy="c.y"
                        :r="vertexRadius(img.key) * (editMode[img.key] ? 2.4 : 1)"
                        :fill="boxColor(bi)"
                        stroke="#fff"
                        :stroke-width="1.5"
                        vector-effect="non-scaling-stroke"
                      />
                    </template>
                  </g>
                  <!-- 新增绘制预览 -->
                  <g v-if="draft.shape && draft.imageIndex === i">
                    <rect
                      v-if="draft.shape === 'rect'"
                      :x="draftRect().x"
                      :y="draftRect().y"
                      :width="draftRect().w"
                      :height="draftRect().h"
                      fill="rgba(64,158,255,0.15)"
                      stroke="#409eff"
                      stroke-dasharray="6 4"
                      :stroke-width="2"
                      vector-effect="non-scaling-stroke"
                    />
                    <template v-else>
                      <!-- 已确定的边：开放折线，不自动连回起点（只有点回起点闭合时才成环） -->
                      <polyline
                        v-if="draftVertices().length >= 2"
                        :points="draftPolyPoints()"
                        fill="none"
                        stroke="#1677ff"
                        stroke-linejoin="round"
                        :stroke-width="3"
                        vector-effect="non-scaling-stroke"
                      />
                      <!-- 橡皮筋：从最后一个顶点到当前光标的实时边 -->
                      <line
                        v-if="draftVertices().length >= 1 && hover.show && hover.key === img.key"
                        :x1="lastDraftVertex().x"
                        :y1="lastDraftVertex().y"
                        :x2="hover.natX"
                        :y2="hover.natY"
                        stroke="#1677ff"
                        stroke-dasharray="6 4"
                        :stroke-width="3"
                        vector-effect="non-scaling-stroke"
                      />
                      <!-- 已确定的顶点：首点为大空心圆（闭合目标），其余为红色实心 -->
                      <template v-for="(v, vi) in draftVertices()" :key="`dp-${vi}`">
                        <circle
                          v-if="vi === 0"
                          class="draft-start-vertex"
                          :class="{ 'draft-start-vertex--close': polygonClosingHint(img.key) }"
                          :cx="v.x"
                          :cy="v.y"
                          :r="vertexRadius(img.key) * (polygonClosingHint(img.key) ? 3.4 : 2.8)"
                          fill="none"
                          stroke="#ff9800"
                          :stroke-width="4"
                          vector-effect="non-scaling-stroke"
                        />
                        <circle
                          v-else
                          :cx="v.x"
                          :cy="v.y"
                          :r="vertexRadius(img.key)"
                          fill="#f5222d"
                          stroke="#fff"
                          :stroke-width="1.5"
                          vector-effect="non-scaling-stroke"
                        />
                      </template>
                      <!-- 光标处的待定顶点：白底 + 黄色光晕 -->
                      <circle
                        v-if="draftVertices().length >= 1 && hover.show && hover.key === img.key"
                        class="draft-cursor-vertex"
                        :cx="hover.natX"
                        :cy="hover.natY"
                        :r="vertexRadius(img.key)"
                        fill="#fff"
                        stroke="#fadb14"
                        :stroke-width="3"
                        vector-effect="non-scaling-stroke"
                      />
                    </template>
                  </g>
                </svg>
              </div>
              <!-- 十字辅助线：铺满整个视口（含图片外），随光标定位 -->
              <div
                v-if="hover.show && hover.key === img.key && !dragging"
                class="crosshair"
              >
                <div class="crosshair-v" :style="{ left: hover.left + 'px' }"></div>
                <div class="crosshair-h" :style="{ top: hover.top + 'px' }"></div>
              </div>
              <!-- 跟随光标的坐标标签：置于未变换的视口内，不随图片缩放/平移漂移 -->
              <div
                v-if="hover.show && hover.key === img.key && !dragging"
                class="coord-label"
                :style="{ left: hover.left + 'px', top: hover.top + 'px' }"
              >
                X: {{ hover.natX }}, Y: {{ hover.natY }}
              </div>
            </div>
          </div>
        </article>
      </div>
    </section>

    <!-- 右：标注信息 -->
    <section class="pane pane-right">
      <header class="pane-head">
        <div class="pane-head-l">
          <span class="pane-title">标注信息</span>
          <span class="hint"> {{ allRows.length }} 项</span>
        </div>
      </header>

      <div class="pane-body ann-list">
        <div v-if="!loading && !allRows.length" class="empty">暂无标注</div>
        <el-alert
          v-else-if="unassignedCount"
          class="ann-warn"
          :title="`${unassignedCount} 个标注未能按 displayKey 匹配到切图，已暂挂在第一张图`"
          type="warning"
          show-icon
          :closable="false"
        />

        <ul v-if="visibleRows.length" class="ann-ul">
          <template v-for="(row, idx) in visibleRows" :key="`${row.imageIndex}-${row.index}`">
            <li
              v-if="idx === 0 || visibleRows[idx - 1].isPolygon !== row.isPolygon"
              class="ann-group"
            >
              {{ row.isPolygon ? '多边形' : '矩形' }}
              <span class="hint">{{ row.isPolygon ? polyCount : rectCount }} 项</span>
            </li>
            <li
              :id="`ann-row-${row.imageIndex}-${row.index}`"
              :class="[
                'ann-item',
                {
                  'ann-item--active': activeBox === row.box,
                  'ann-item--flash': activeBox === row.box && flashSource === 'img',
                  'ann-item--poly': row.isPolygon,
                },
              ]"
              @mouseenter="hoverBox = row.box"
              @mouseleave="hoverBox = null"
              @click="focusRow(row)"
            >
              <div class="ann-main">
                <div class="ann-line">
                  <span class="ann-no">{{ row.no }}</span>
                  <span class="ann-chip">
                    <span class="ann-dot" :style="{ background: row.color }"></span>
                    {{ row.name }}
                  </span>
                  <span v-if="!isSingleImage" class="ann-img-hint" :title="row.imageName">📄 {{ row.imageName }}</span>
                </div>
                <div v-if="row.fields.length" class="ann-line ann-line--sub">
                  <span v-for="f in row.fields" :key="f.label" class="ann-field">
                    <span class="ann-key">{{ f.label }}</span
                    ><span class="ann-sep">: </span><span class="ann-val">{{ f.value }}</span>
                  </span>
                </div>
              </div>
              <span class="ann-actions">
                <el-button link type="primary" size="small" :icon="EditIcon" @click.stop="startEdit(row)"></el-button>
                <el-button
                  link
                  type="danger"
                  size="small"
                  class="ann-del-btn"
                  :icon="DeleteIcon"
                  @click.stop="deleteBox(row)"
                ></el-button>
              </span>
            </li>
          </template>
        </ul>
      </div>
    </section>

    <!-- 标注编辑弹框：类型单选、其余文本可编辑；多边形通常只有类型 -->
    <el-dialog v-model="editDialog" title="编辑标注" width="880px" top="8vh" append-to-body>
      <el-form label-width="88px">
        <el-form-item label="名称">
          <el-input :model-value="editBox?.name || ''" disabled />
        </el-form-item>
        <el-form-item v-if="editHasType" label="类型">
          <div class="type-radios">
            <el-radio-group v-model="editType">
              <el-radio v-for="o in typeOptions" :key="o.code" :value="o.code">{{ o.label }}</el-radio>
            </el-radio-group>
          </div>
        </el-form-item>
        <div v-if="editFields.length" class="attr-grid">
          <el-form-item v-for="ef in editFields" :key="ef.key" :label="ef.label">
            <el-input v-model="ef.value" clearable />
          </el-form-item>
        </div>
        <div v-if="!editHasType && !editFields.length" class="edit-empty">该标注无可编辑属性</div>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新增标注弹框：几何已在图上绘制，此处选类型(+数值)并写回 -->
    <el-dialog v-model="addDialog" title="新增标注" width="880px" top="8vh" append-to-body>
      <el-form label-width="88px">
        <el-form-item label="形状">
          <el-tag :type="addIsPolygon ? 'warning' : 'primary'">{{ addIsPolygon ? '多边形' : '矩形' }}</el-tag>
        </el-form-item>
        <el-form-item label="类型">
          <div class="type-radios">
            <el-radio-group v-model="addType">
              <el-radio v-for="o in addTypeOptions" :key="o.code" :value="o.code">{{ o.label }}</el-radio>
            </el-radio-group>
          </div>
        </el-form-item>
        <div v-if="!addIsPolygon" class="attr-grid">
          <el-form-item v-for="f in CAD_RECT_ATTR_FIELDS" :key="f.key" :label="f.label">
            <el-input v-model="addAttrs[f.key]" :placeholder="`请输入${f.label}（可选）`" clearable />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="cancelAdd">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!addType" @click="submitAdd">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cad-page {
  display: flex;
  gap: 16px;
  height: calc(100vh - var(--app-header-height));
  padding: 16px;
}

.pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  overflow: hidden;
}

.pane-left {
  flex: 3;
}

.pane-right {
  flex: 1;
  min-width: 350px;
  max-width: 540px;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: none;
  height: 48px;
  padding: 0 16px;
  border-bottom: 1px solid #ebeef5;
}

.pane-title {
  font-size: 15px;
  font-weight: 600;
}

.hint {
  font-size: 12px;
  color: #a8abb2;
}

/* 右栏头部：标题+计数居左，新增按钮居右 */
.pane-head-l {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

/* 卡片头的“新增”按钮组：用标签 + 虚线框把矩形/多边形绑定成一个新增入口 */
.add-group {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 6px 2px 9px;
  border: 1px dashed #a0cfff;
  border-radius: 999px;
  background: #f0f7ff;
}
/* el-button 相邻默认自带 12px margin-left，与 gap 叠加会过大，归零后仅由 gap 控制间距 */
.add-group .el-button + .el-button {
  margin-left: 0;
}
/* 卡片头的“编辑”按钮组：与新增同款胶囊，橙色系区分职责 */
.edit-group {
  border-color: #f3cfa0;
  background: #fdf6ec;
}
.edit-group .add-group-label {
  color: #e6a23c;
}
.add-group-label {
  font-size: 12px;
  font-weight: 600;
  color: #409eff;
  white-space: nowrap;
}

/* 卡片内绘制提示条 */
.draw-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: #ecf5ff;
  border-bottom: 1px solid #d9ecff;
  font-size: 12px;
}
.draw-tip {
  flex: 1;
  min-width: 0;
  color: #409eff;
}

/* 几何编辑提示条：与绘制条同构，橙色系呼应编辑按钮组 */
.edit-bar {
  background: #fdf6ec;
  border-bottom-color: #faecd8;
}
.edit-bar .draw-tip {
  color: #b88230;
}

/* 编辑模式下可拖拽的顶点手柄（叠加层整体 pointer-events:none，仅作视觉提示） */
.vertex-handle--drag {
  stroke: #303133;
  stroke-width: 2;
}

/* 新增弹框：形状旁的说明 */
.add-hint {
  margin-left: 10px;
  font-size: 12px;
  color: #a8abb2;
}

/* 多边形绘制：光标处待定顶点的黄色光晕 */
.draft-cursor-vertex {
  filter: drop-shadow(0 0 6px rgba(250, 219, 20, 0.95));
}

/* 多边形起点大空心圆：亮橘黄，悬停时高亮提示“点击闭合” */
.draft-start-vertex {
  cursor: pointer;
  filter: drop-shadow(0 0 4px rgba(255, 152, 0, 0.7));
}
.draft-start-vertex--close {
  filter: drop-shadow(0 0 9px rgba(255, 152, 0, 0.95));
}

.cad-path {
  flex: none;
  padding: 8px 16px;
  font-size: 13px;
  color: #606266;
  border-bottom: 1px solid #f0f0f0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 叠加层工具条 */
.cad-toolbar {
  flex: none;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafbfc;
}

.toolbar-hint {
  margin-left: auto;
  font-size: 12px;
  color: #a8abb2;
}

/* 工具条左侧的公共路径前缀（/images/ 之前那段） */
.toolbar-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #909399;
}

.pane-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* 左：切图卡片 */
.img-list {
  padding: 12px 16px;
}

.img-card {
  margin-bottom: 16px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  cursor: pointer;
}

.img-card--active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.img-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid #f0f0f0;
}

.img-path {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.img-meta {
  flex: none;
  font-size: 12px;
  color: #909399;
}

/* 单图标注页入口：卡片头一个小文字链接，新标签打开仅含当前图片的页面 */
.img-single-link {
  flex: none;
  font-size: 12px;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 10px;
  color: #409eff;
  background: #ecf5ff;
  text-decoration: none;
  white-space: nowrap;
  border: 1px solid #d9ecff;
}

.img-single-link:hover {
  color: #fff;
  background: #409eff;
  border-color: #409eff;
}

.img-meta em {
  font-style: normal;
  color: #0ea5e9;
}

.img-card-body {
  padding: 12px;
  background: #e8eaed;
}

/* 视口：缩放/平移在此框内裁剪，固定高度使“拖动”有意义；背景置灰与白底图纸区分 */
.img-stage {
  position: relative;
  overflow: hidden;
  height: 62vh;
  min-height: 320px;
  background: #e8eaed;
}

/* 包裹 img 与叠加层，受 translate+scale 变换；以左上角为缩放/平移基准 */
.img-wrap {
  position: relative;
  width: 100%;
  transform-origin: 0 0;
  will-change: transform;
}

.img-el {
  display: block;
  width: 100%;
  height: auto;
  user-select: none;
}

.img-overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

/* 框本体不参与命中：命中统一由舞台层按自然坐标算（矩形 fill=none 也能点中） */
.box-shape {
  pointer-events: none;
}

/* 选中框闪烁：仅限从右栏选中时（用户视线在列表），刚被选中时吸引注意，跑完 3 次即停 */
.box-shape--flash {
  animation: box-flash 0.55s ease-in-out 3;
}
@keyframes box-flash {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.15;
  }
}

/* 高亮框中心的类型/数值标签：白描边保证在图纸上可读 */
.box-label {
  pointer-events: none;
  font-weight: 600;
  paint-order: stroke;
  stroke: #fff;
  stroke-width: 4px;
  stroke-linejoin: round;
}

/* 跟随光标的坐标标签（红色，与截图一致） */
.coord-label {
  position: absolute;
  z-index: 2;
  transform: translate(10px, -50%);
  padding: 0 2px;
  font-size: 13px;
  color: #ef4444;
  white-space: nowrap;
  pointer-events: none;
  text-shadow: 0 0 2px #fff, 0 0 2px #fff;
}

/* 十字辅助线：铺满整个视口，虚线，不拦截鼠标 */
.crosshair {
  position: absolute;
  inset: 0;
  z-index: 1;
  overflow: hidden;
  pointer-events: none;
}

.crosshair-v {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1px dashed #b0b3b8;
}

.crosshair-h {
  position: absolute;
  left: 0;
  right: 0;
  height: 0;
  border-top: 1px dashed #b0b3b8;
}

/* 右：标注列表 */
.ann-list {
  padding: 0 0 8px;
  /* 预留吸顶分组头的高度：scrollIntoView 遇顶行时停在头下方，不被盖住 */
  scroll-padding-top: 40px;
}

.ann-warn {
  margin: 0 12px 8px;
}

.ann-ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.ann-item {
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 14px;
  font-size: 13px;
  cursor: pointer;
  border-left: 3px solid transparent;
  border-bottom: 1px solid #eef0f3;
}

/* 左侧主区：名称行 + 属性行，宽度自适应，属性多时在此列内换行，不会钻到右侧按钮下方 */
.ann-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* 多边形行用淡蓝底 + 虚线左边，与矩形区分 */
.ann-item--poly {
  background: #f6fbff;
  border-left-color: #bfdbfe;
}

.ann-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.ann-line--sub {
  padding-left: 30px;
  font-size: 12px;
}

.ann-item:hover {
  background: #f5f7fa;
}

.ann-item--active {
  background: #ecf5ff;
  /* 选中行加一圈粗边框更突出；用 outline 不改变盒模型、无布局抖动 */
  outline: 2px solid #409eff;
  outline-offset: -2px;
  border-radius: 2px;
}

/* 图上点框联动选中时：闪烁对应列表行，提示视线回到列表 */
.ann-item--flash {
  animation: ann-flash 0.55s ease-in-out 3;
}
@keyframes ann-flash {
  0%,
  100% {
    background: #ecf5ff;
    box-shadow: none;
  }
  50% {
    background: #d9ecff;
    box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.4);
  }
}

.ann-group {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  background: #f5f7fa;
  border-top: 1px solid #ebeef5;
  border-bottom: 1px solid #ebeef5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ann-no {
  width: 20px;
  color: #909399;
  text-align: right;
}

/* 按形状分组后，行内保留所属图片名以便回溯：做成浅蓝胶囊徽标，一眼可辨 */
.ann-img-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 60%;
  padding: 1px 8px;
  font-size: 12px;
  font-weight: 600;
  color: #337ecc;
  background: #eef4fd;
  border: 1px solid #cfe3f8;
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ann-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 88px;
  padding: 2px 8px;
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}

.ann-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.ann-badge {
  flex: none;
  padding: 0 6px;
  font-size: 12px;
  line-height: 18px;
  color: #909399;
  border: 1px solid #dcdfe6;
  border-radius: 3px;
}

.ann-badge--poly {
  color: #0ea5e9;
  border-color: #7dd3fc;
  border-style: dashed;
}

.ann-field {
  color: #909399;
}

/* 键（字段名）统一蓝色，值统一深灰 */
.ann-key {
  color: #409eff;
}

.ann-sep {
  color: #909399;
}

.ann-val {
  color: #303133;
}

.ann-value {
  color: #409eff;
}

/* 行右侧操作区 */
.ann-actions {
  margin-left: auto;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

/* 垃圾桶图标视觉上比编辑铅笔小，单独放大一点对齐 */
.ann-del-btn :deep(.el-icon) {
  font-size: 1.3em;
}

/* 类型选项较多：不限高、不内部滚动，用两列网格全部展开，避免选到后面的类型要滚动才看到 */
.type-radios {
  width: 100%;
}

/* 数值等属性字段两列排布 */
.attr-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 16px;
}

.type-radios :deep(.el-radio-group) {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 6px 12px;
  width: 100%;
}

.type-radios :deep(.el-radio) {
  margin-right: 0;
  height: auto;
  white-space: normal;
  align-items: flex-start;
}

.type-radios :deep(.el-radio__label) {
  white-space: normal;
  line-height: 1.4;
}

.edit-empty {
  padding: 4px 0 0 88px;
  font-size: 13px;
  color: #909399;
}

.empty {
  padding: 24px;
  text-align: center;
  color: #a8abb2;
}
</style>
