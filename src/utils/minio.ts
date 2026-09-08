/**
 * 文档 / 标注 json 读取封装（前端侧）。
 *
 * md 原文与三个标注 json 都通过后端下载接口获取（直连，不走代理）：
 *   GET <VITE_API_DIRECT_BASE>/file/download?fullObjectPath=/<bucket>/<key>（fullObjectPath 需 URL 编码）
 * 前端不持有任何 MinIO 密钥；桶名（VITE_MINIO_BUCKET）仅用于拼 fullObjectPath 前缀。
 *
 * 路径规则不变：视图拿到 md 的 key 后，用 deriveAnnotationKeys 推出 QA/SFT/CoT 三个 json 的 key，
 * 再分别调 getObjectText 去后端下载。
 */

/** 存储桶名称：仅用于拼下载/上传接口需要的 fullObjectPath 前缀（/<bucket>/<key>） */
const MINIO_BUCKET = import.meta.env.VITE_MINIO_BUCKET || ''

/** 后端文件接口直连基址（含 /api），md 与标注 json 都从 /file/download 取；不走代理 */
const API_DIRECT_BASE = (import.meta.env.VITE_API_DIRECT_BASE || '/api').replace(/\/$/, '')

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
 * 按对象 key 读取文本内容（md 原文 / 标注 json），走后端下载接口直连。
 * @param key 桶内对象路径（不含桶名），例如 'a/b/c.md'；也兼容带桶名前缀的完整路径
 * @returns   对象的 utf-8 文本
 */
export async function getObjectText(key: string): Promise<string> {
  if (!isMinioConfigured()) {
    throw new Error('MinIO 未配置（检查 .env 里的 VITE_MINIO_BUCKET）')
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
