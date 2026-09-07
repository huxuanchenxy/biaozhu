/**
 * MinIO 对象存储读取封装（前端侧，服务端签名架构）。
 *
 * 前端不再签名、也不持有任何密钥：只发「同源 GET /<bucket>/<key>」。
 * 真正的 SigV4 签名由服务端完成：
 *   - 生产：server.mjs（与静态文件同进程）签名后转发 MinIO；
 *   - 开发：Vite 把 /<bucket>/... 代理到本地 server.mjs（签名服务），再由它转发 MinIO。
 * 这样浏览器时钟偏差不会再触发 RequestTimeTooSkewed，密钥也不会进前端包。
 *
 * 视图层只需调用 getObjectText(key) 按「桶内对象路径」拿到文本内容。
 */

/** 存储桶名称（前端唯一需要的 MinIO 配置，用于拼同源路径） */
const MINIO_BUCKET = import.meta.env.VITE_MINIO_BUCKET || ''

/**
 * 严格编码单个路径段：encodeURIComponent 之后再补上它默认不编码的 !'()*。
 * MinIO 计算 SigV4 canonical URI 时会对路径规范化（字面 '(' 会变成 %28），
 * 前端直接发出严格编码的路径，可保证「线上路径 == 签名 canonical」，避免签名不匹配。
 */
function encodePathSegment(seg: string): string {
  return encodeURIComponent(seg).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase())
}

/**
 * 规范化对象 key：
 *   - 去掉开头的 '/'（S3 key 不以 / 开头）
 *   - 若 URL 里带上了桶名前缀（如 /drivdernet_abc/xxx），自动剥掉桶名
 * 注意：这里**不**折叠重复斜杠。对象 key 可能真实包含空目录段（'a//b'，
 * 即 MinIO 里名为 '/' 的空文件夹），折叠会指向一个不存在的 key。
 * 例：'/drivdernet_abc/a/b.md' -> 'a/b.md'
 */
export function normalizeObjectKey(raw: string): string {
  let key = (raw || '').replace(/^\/+/, '')
  if (MINIO_BUCKET && (key === MINIO_BUCKET || key.startsWith(`${MINIO_BUCKET}/`))) {
    key = key.slice(MINIO_BUCKET.length).replace(/^\/+/, '')
  }
  return key
}

/**
 * 由 md 的 object key 推导三个标注 json 的 object key。
 *
 * 目录规则（QA / SFT / CoT 三个文件夹名固定，与 MD 同级）：
 *   md      : <parent>/MD/<name>.md
 *   qa      : <parent>/QA/<name>_qa.json
 *   alpaca  : <parent>/SFT/<name>_sft.json
 *   cot     : <parent>/CoT/<name>_cot.json
 * 其中 <parent> 是 MD 文件夹的上一级，<name> 是 md 文件名（去掉 .md 扩展名）。
 * 文件夹名固定，里面的文件名随 <name> 变，故按同一 <name> 拼接后缀即可。
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
  return {
    qa: `${prefix}QA/${name}_qa.json`,
    alpaca: `${prefix}SFT/${name}_sft.json`,
    cot: `${prefix}CoT/${name}_cot.json`,
  }
}

/** 是否已具备访问 MinIO 的必要配置（前端只需桶名） */
export function isMinioConfigured(): boolean {
  return Boolean(MINIO_BUCKET)
}

/**
 * 拼出后端 uploadOverwrite 接口需要的完整对象路径：/<bucket>/<key>（含桶名前缀与开头斜杠，空格等保持原样不编码）。
 * 例：key='a/b/Cot/x.json' -> '/drivdernet_abc/a/b/Cot/x.json'
 */
export function fullObjectPath(key: string): string {
  return `/${MINIO_BUCKET}/${normalizeObjectKey(key)}`
}

/**
 * 按对象 key 读取 MinIO 中的文本内容（如 md 原文）。
 * 走同源请求，由服务端签名服务转发 MinIO。
 * @param key 桶内对象路径（不含桶名），例如 'a/b/c.md'
 * @returns   对象的 utf-8 文本
 */
export async function getObjectText(key: string): Promise<string> {
  if (!isMinioConfigured()) {
    throw new Error('MinIO 未配置（检查 .env 里的 VITE_MINIO_BUCKET）')
  }
  const objectKey = normalizeObjectKey(key)
  if (!objectKey) throw new Error('对象路径为空')
  // 空目录段（连续 '//'）是数据问题：MinIO 的 S3 路由会归一化重复斜杠，
  // 导致「签名用的路径」与「实际查找的路径」不一致（SignatureDoesNotMatch / NoSuchKey）。
  // 这里提前拦截并给出可操作的提示，避免暴露难懂的签名错误。
  if (objectKey.includes('//')) {
    throw new Error(
      `对象路径含空目录段（连续 "//"）：${objectKey}。MinIO 无法通过 S3 接口直接读取该对象，请先在 MinIO 中修正对象 key（去掉空目录）后重试。`,
    )
  }

  const url = `/${MINIO_BUCKET}/` + objectKey.split('/').map(encodePathSegment).join('/')
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`读取 MinIO 对象失败 HTTP ${res.status}：${objectKey}`)
  }
  return await res.text()
}
