/**
 * 文档 / 标注 json 读取封装（前端侧）。
 *
 * md 原文与三个标注 json 都通过后端下载接口获取（直连，不走代理）：
 *   GET <VITE_API_DIRECT_BASE>/file/download?fullObjectPath=/<bucket>/<key>（fullObjectPath 需 URL 编码）
 * 前端不持有任何 MinIO 密钥；桶名不再写死于环境变量，而是**从 URL 动态取**——
 * 路由约定 /markdown/<bucket>/<objectKey>，即路由 key 的第一段就是桶名，整体即 fullObjectPath 的主体。
 *
 * 路径规则不变：视图拿到 md 的 key（含桶名）后，用 deriveAnnotationKeys 推出 QA/SFT/CoT 三个 json 的 key，
 * 再分别调 getObjectText 去后端下载。
 */

/** 后端文件接口直连基址（含 /api），md 与标注 json 都从 /file/download 取；不走代理 */
const API_DIRECT_BASE = (import.meta.env.VITE_API_DIRECT_BASE || '/api').replace(/\/$/, '')

/**
 * 三个标注 json 的相对路径模板（相对 md 的上级目录 <parent>，与 QA/SFT/CoT 文件夹同级）。
 * {name} 会替换成 md 主名（去掉 .md 扩展名）。后缀（如 _sft.json）或文件夹名变了，
 * 只改 .env 里对应配置即可，无需动代码。
 */
const QA_PATTERN = import.meta.env.VITE_ANNOTATION_QA_PATTERN || 'QA/{name}_qa.json'
const SFT_PATTERN = import.meta.env.VITE_ANNOTATION_SFT_PATTERN || 'SFT/{name}_sft.json'
const COT_PATTERN = import.meta.env.VITE_ANNOTATION_COT_PATTERN || 'CoT/{name}_cot.json'

/**
 * SFT 模板按桶区分（可选）：格式 `<bucket>=<pattern>`，多条用 `|` 分隔。
 * 例：materialsproject=SFT/{name}_alpaca.json|drivdernet_abc=SFT/{name}_sft.json
 * 未命中的桶回退到 SFT_PATTERN（VITE_ANNOTATION_SFT_PATTERN）。
 */
function parseBucketPatterns(raw?: string): Record<string, string> {
  const map: Record<string, string> = {}
  if (!raw) return map
  for (const entry of raw.split('|')) {
    const eq = entry.indexOf('=')
    if (eq <= 0) continue
    const bucket = entry.slice(0, eq).trim()
    const pattern = entry.slice(eq + 1).trim()
    if (bucket && pattern) map[bucket] = pattern
  }
  return map
}

const SFT_PATTERN_BY_BUCKET = parseBucketPatterns(
  import.meta.env.VITE_ANNOTATION_SFT_PATTERN_BY_BUCKET,
)

/**
 * 原始 PDF 的相对路径模板：相对 md 上级目录 <parent> 的上一级（即与 <parent> 同级），
 * {name} 替换为 md 主名（去 .md）。默认 <grandparent>/{name}.pdf。
 * 例：.../成品语料/<id>/MD/<id>.md -> .../成品语料/<id>.pdf。规则变动只改 .env。
 */
const PDF_PATTERN = import.meta.env.VITE_DOC_PDF_PATTERN || '{name}.pdf'

/** 译文 md 的文件名前缀：与原文同目录，仅在文件名前加此前缀（默认 cn_）。可用 .env 覆盖。 */
const TRANSLATED_MD_PREFIX = import.meta.env.VITE_DOC_TRANSLATED_PREFIX || 'cn_'

/** 把模板里的 {name} 替换成 md 主名，并拼上 <parent>/ 前缀 */
function applyPattern(pattern: string, prefix: string, name: string): string {
  return `${prefix}${pattern.replace(/\{name\}/g, name)}`
}

/**
 * 规范化对象完整路径（含桶名）：仅去掉开头的 '/'。
 * 桶名来自 URL（路由 key 的第一段），此处**不再**按环境变量剥离桶名，
 * 否则会把 materialsproject 这类真实桶名当成普通目录再前置写死的桶，导致路径出错。
 * 注意：这里**不**折叠重复斜杠。对象 key 可能真实包含空目录段（'a//b'，
 * 即 MinIO 里名为 '/' 的空文件夹），折叠会指向一个不存在的 key。
 * 例：'/materialsproject/a/b.md' -> 'materialsproject/a/b.md'
 */
export function normalizeObjectKey(raw: string): string {
  return (raw || '').replace(/^\/+/, '')
}

/**
 * 由 md 的 object key 推导三个标注 json 的 object key。
 *
 * md 位于 <parent>/MD/<name>.md；三个 json 的相对路径由 .env 里的模板决定
 * （见 QA_PATTERN/SFT_PATTERN/COT_PATTERN），模板相对 <parent>，{name} 替换为 md 主名。
 * 默认：QA/{name}_qa.json、SFT/{name}_sft.json、CoT/{name}_cot.json。
 * SFT 模板可按桶区分（VITE_ANNOTATION_SFT_PATTERN_BY_BUCKET）：桶名即 key 第一段，
 * 命中则用该桶的模板，否则回退到 SFT_PATTERN。后缀或文件夹名变动时，只改 .env 配置即可。
 */
export function deriveAnnotationKeys(mdKey: string): {
  qa: string
  alpaca: string
  cot: string
} {
  const key = normalizeObjectKey(mdKey).replace(/\\/g, '/')
  const slash = key.lastIndexOf('/')
  const mdDir = slash >= 0 ? key.slice(0, slash) : '' // <parent>/MD
  const parentSlash = mdDir.lastIndexOf('/')
  const parent = parentSlash >= 0 ? mdDir.slice(0, parentSlash) : '' // <parent>
  const fileName = slash >= 0 ? key.slice(slash + 1) : key // <name>.md
  const name = fileName.replace(/\.md$/i, '') // <name>
  const prefix = parent ? `${parent}/` : ''
  // 桶名即 key 第一段（来自 URL /markdown/<bucket>/<objectKey>），用于按桶选择 SFT 模板
  const bucketSlash = key.indexOf('/')
  const bucket = bucketSlash >= 0 ? key.slice(0, bucketSlash) : key
  const sftPattern = SFT_PATTERN_BY_BUCKET[bucket] || SFT_PATTERN
  return {
    qa: applyPattern(QA_PATTERN, prefix, name),
    alpaca: applyPattern(sftPattern, prefix, name),
    cot: applyPattern(COT_PATTERN, prefix, name),
  }
}

/**
 * 由 md 的 object key 推导原始 PDF 的 object key。
 *
 * md 位于 <parent>/MD/<name>.md（<parent> 是装 MD 文件夹的目录，其目录名一般即文档 id）。
 * 原始 PDF 与 <parent> 同级、以文档名 + .pdf 命名，即 <parent 的上级目录>/<name>.pdf。
 * 例：.../成品语料/<id>/MD/<id>.md -> .../成品语料/<id>.pdf
 * 规则可用 .env 的 VITE_DOC_PDF_PATTERN 覆盖（相对 <parent> 的上级目录，{name}=md 主名）。
 */
export function derivePdfKey(mdKey: string): string {
  const key = normalizeObjectKey(mdKey).replace(/\\/g, '/')
  const slash = key.lastIndexOf('/')
  const mdDir = slash >= 0 ? key.slice(0, slash) : '' // <parent>/MD
  const parentSlash = mdDir.lastIndexOf('/')
  const parent = parentSlash >= 0 ? mdDir.slice(0, parentSlash) : '' // <parent>（装 MD 的目录）
  const grandSlash = parent.lastIndexOf('/')
  const grandparent = grandSlash >= 0 ? parent.slice(0, grandSlash) : '' // <parent> 的上级
  const fileName = slash >= 0 ? key.slice(slash + 1) : key // <name>.md
  const name = fileName.replace(/\.md$/i, '') // <name>
  const prefix = grandparent ? `${grandparent}/` : ''
  return applyPattern(PDF_PATTERN, prefix, name)
}

/**
 * 由 md 的 object key 推导「译文 md」的 object key：与原文位于同一目录，
 * 仅在文件名前加前缀（默认 cn_）。例：.../MD/<name>.md -> .../MD/cn_<name>.md
 * 前缀可用 .env 的 VITE_DOC_TRANSLATED_PREFIX 覆盖。
 */
export function deriveTranslatedMdKey(mdKey: string): string {
  const key = normalizeObjectKey(mdKey).replace(/\\/g, '/')
  const slash = key.lastIndexOf('/')
  const dir = slash >= 0 ? key.slice(0, slash + 1) : '' // 含结尾 '/'
  const fileName = slash >= 0 ? key.slice(slash + 1) : key
  return `${dir}${TRANSLATED_MD_PREFIX}${fileName}`
}

/**
 * 是否具备读取条件：后端直连基址已配置即可。
 * 桶名由 URL 动态提供，不再依赖环境变量，故此判断只校验直连基址。
 */
export function isMinioConfigured(): boolean {
  return Boolean(API_DIRECT_BASE)
}

/**
 * 拼出后端 download/uploadOverwrite 接口需要的完整对象路径：/<bucket>/<key>（含开头斜杠，空格等保持原样不编码）。
 * 桶名即传入 key 的第一段（来自 URL），不再前置写死的环境变量桶名。
 * 例：key='materialsproject/a/CoT/x.json' -> '/materialsproject/a/CoT/x.json'
 */
export function fullObjectPath(key: string): string {
  return `/${normalizeObjectKey(key)}`
}

/**
 * 按对象 key 读取文本内容（md 原文 / 标注 json），走后端下载接口直连。
 * @param key 桶内对象路径（不含桶名），例如 'a/b/c.md'；也兼容带桶名前缀的完整路径
 * @returns   对象的 utf-8 文本
 */
export async function getObjectText(key: string): Promise<string> {
  if (!isMinioConfigured()) {
    throw new Error('后端文件直连基址未配置（检查 .env 里的 VITE_API_DIRECT_BASE）')
  }
  const objectKey = normalizeObjectKey(key)
  if (!objectKey) throw new Error('对象路径为空')
  // 空目录段（连续 '//'）通常是数据问题，提前拦截并给出可操作提示
  if (objectKey.includes('//')) {
    throw new Error(
      `对象路径含空目录段（连续 "//"）：${objectKey}。请先修正对象 key（去掉空目录）后重试。`,
    )
  }

  // fullObjectPath = /<bucket>/<key>，整体 URL 编码后作为 query 传给后端下载接口
  const full = fullObjectPath(objectKey)
  const url = `${API_DIRECT_BASE}/file/download?fullObjectPath=${encodeURIComponent(full)}`
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`读取文件失败 HTTP ${res.status}：${objectKey}`)
  }
  return await res.text()
}

/**
 * 按对象 key 读取二进制内容并生成 blob: 对象 URL（用于 PDF 等在 <iframe> 里直接预览）。
 * 走与 getObjectText 相同的后端下载接口，只是取 arrayBuffer 并强制标记 MIME，
 * 避免后端未返回正确 Content-Type 时浏览器把 PDF 当下载或纯文本处理。
 * 注意：调用方在不再需要该 URL（切换文档 / 组件卸载）时必须 URL.revokeObjectURL 释放。
 */
export async function getObjectBlobUrl(key: string, mime = 'application/pdf'): Promise<string> {
  if (!isMinioConfigured()) {
    throw new Error('后端文件直连基址未配置（检查 .env 里的 VITE_API_DIRECT_BASE）')
  }
  const objectKey = normalizeObjectKey(key)
  if (!objectKey) throw new Error('对象路径为空')
  if (objectKey.includes('//')) {
    throw new Error(
      `对象路径含空目录段（连续 "//"）：${objectKey}。请先修正对象 key（去掉空目录）后重试。`,
    )
  }
  const full = fullObjectPath(objectKey)
  const url = `${API_DIRECT_BASE}/file/download?fullObjectPath=${encodeURIComponent(full)}`
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`读取文件失败 HTTP ${res.status}：${objectKey}`)
  }
  const buffer = await res.arrayBuffer()
  return URL.createObjectURL(new Blob([buffer], { type: mime }))
}
