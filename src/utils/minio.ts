/**
 * MinIO 对象存储读取封装（方案 A：@aws-sdk/client-s3）。
 *
 * MinIO 兼容 S3 协议，这里用 AWS SDK v3 的 S3Client 直连内网 MinIO：
 *   - forcePathStyle: true  —— MinIO 必须用 path 风格（http://host/bucket/key），不能用子域名风格
 *   - 凭证来自 .env 的 VITE_MINIO_*（打包后会明文进前端，仅限内网/内部工具使用）
 *
 * 视图层只需调用 getObjectText(key) 按「桶内对象路径」拿到文本内容。
 */
import { GetObjectCommand, S3Client } from '@aws-sdk/client-s3'

/** MinIO 配置（来自 .env，均以 VITE_ 开头才会打包进前端） */
const MINIO_ENDPOINT = (import.meta.env.VITE_MINIO_ENDPOINT || '').replace(/\/$/, '')
const MINIO_ACCESS_KEY = import.meta.env.VITE_MINIO_ACCESS_KEY || ''
const MINIO_SECRET_KEY = import.meta.env.VITE_MINIO_SECRET_KEY || ''
const MINIO_BUCKET = import.meta.env.VITE_MINIO_BUCKET || ''
/**
 * 是否走「同源反向代理」访问 MinIO（dev: Vite server.proxy；prod: Nginx）。
 * 浏览器直连 MinIO 属于跨域，需 MinIO 侧开 CORS；开不了就置 true 走代理绕过。
 */
const MINIO_USE_PROXY = String(import.meta.env.VITE_MINIO_USE_PROXY ?? '').toLowerCase() === 'true'

/**
 * 解析 SDK 实际请求的 endpoint：
 *   - 走代理：请求同源地址（window.location.origin），由 Vite/Nginx 把 /<bucket>/... 转发到真实 MinIO。
 *     同源不触发 CORS；且 Host 与路径都不变，SigV4 签名依旧校验通过。
 *   - 直连：用 .env 里的 VITE_MINIO_ENDPOINT（要求 MinIO 已开 CORS）。
 */
function resolveEndpoint(): string {
  if (MINIO_USE_PROXY && typeof window !== 'undefined') return window.location.origin
  return MINIO_ENDPOINT
}

/** S3 客户端单例：首次使用时按当前 env 初始化，之后复用 */
let client: S3Client | null = null

function getClient(): S3Client {
  if (!client) {
    client = new S3Client({
      // MinIO 不校验 region，随便填一个合法值即可
      region: 'us-east-1',
      endpoint: resolveEndpoint(),
      // MinIO 走 path 风格访问：endpoint/bucket/key
      forcePathStyle: true,
      credentials: {
        accessKeyId: MINIO_ACCESS_KEY,
        secretAccessKey: MINIO_SECRET_KEY,
      },
    })
  }
  return client
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
 *   alpaca  : <parent>/SFT/<name>_alpaca.json
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
    alpaca: `${prefix}SFT/${name}_alpaca.json`,
    cot: `${prefix}CoT/${name}_cot.json`,
  }
}

/** 是否已具备访问 MinIO 的必要配置 */
export function isMinioConfigured(): boolean {
  return Boolean(
    MINIO_ACCESS_KEY && MINIO_SECRET_KEY && MINIO_BUCKET && (MINIO_ENDPOINT || MINIO_USE_PROXY),
  )
}

/**
 * 按对象 key 读取 MinIO 中的文本内容（如 md 原文）。
 * @param key 桶内对象路径（不含桶名），例如 'a/b/c.md'
 * @returns   对象的 utf-8 文本
 */
export async function getObjectText(key: string): Promise<string> {
  if (!isMinioConfigured()) {
    throw new Error('MinIO 未配置（检查 .env 里的 VITE_MINIO_* 变量）')
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

  const res = await getClient().send(
    new GetObjectCommand({ Bucket: MINIO_BUCKET, Key: objectKey }),
  )
  if (!res.Body) throw new Error(`对象无内容：${objectKey}`)
  return await res.Body.transformToString('utf-8')
}
