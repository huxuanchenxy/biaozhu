/**
 * 生产/开发一体化服务器：静态文件 + MinIO 签名反向代理（零依赖，Node 内置模块）。
 *
 * 架构：浏览器 --(无签名, 同源)--> 本服务 --(SigV4 签名, 服务端时钟)--> MinIO
 *
 * 相比「浏览器签名 + 透明代理」的三大好处：
 *   1. 签名在服务端完成，浏览器时钟偏差不会再触发 RequestTimeTooSkewed；
 *      若服务端与 MinIO 仍有时差，读取 MinIO 响应 Date 头自动校正后重试一次。
 *   2. 签名请求由本服务直连 MinIO（Host=MinIO），不再依赖中间代理保留 Host/路径。
 *   3. 密钥只存在于服务端配置（.env，不带 VITE_ 前缀），不进入前端包。
 *
 * 用法（在 dist 目录下，即含 index.html/assets 的目录）：
 *   node server.mjs
 * 配置（读运行目录 .env / .env.production，或启动环境变量，环境变量优先）：
 *   SERVER_PORT / MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET(或 VITE_MINIO_BUCKET) / DIST
 */
import http from 'node:http'
import crypto from 'node:crypto'
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

// 默认服务当前工作目录（即你运行 node server.mjs 时所在的 dist 目录）
const DIST = path.resolve(process.env.DIST || process.cwd())

/** 极简 .env 解析（KEY = VALUE，忽略注释/空行/包裹引号），不挑 VITE_ 前缀 */
function loadEnvFile(file) {
  const out = {}
  let text = ''
  try {
    text = readFileSync(file, 'utf8')
  } catch {
    return out
  }
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/)
    if (!m) continue
    let v = m[2].trim()
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    out[m[1]] = v
  }
  return out
}
const fileEnv = {
  ...loadEnvFile(path.resolve(DIST, '.env')),
  ...loadEnvFile(path.resolve(DIST, '.env.production')),
}

// 监听端口：环境变量 PORT > .env 文件 SERVER_PORT > 默认 3344
const PORT = Number(process.env.PORT || fileEnv.SERVER_PORT || 3344)
// MinIO 配置：环境变量 > .env 文件（MINIO_* 优先，兼容旧的 VITE_MINIO_*）> 硬编码默认
const MINIO_ENDPOINT =
  process.env.MINIO_ENDPOINT || fileEnv.MINIO_ENDPOINT || fileEnv.VITE_MINIO_ENDPOINT || 'http://10.89.14.234:12001'
const MINIO_ACCESS_KEY =
  process.env.MINIO_ACCESS_KEY || fileEnv.MINIO_ACCESS_KEY || fileEnv.VITE_MINIO_ACCESS_KEY || ''
const MINIO_SECRET_KEY =
  process.env.MINIO_SECRET_KEY || fileEnv.MINIO_SECRET_KEY || fileEnv.VITE_MINIO_SECRET_KEY || ''
const BUCKET =
  process.env.MINIO_BUCKET || fileEnv.MINIO_BUCKET || fileEnv.VITE_MINIO_BUCKET || 'drivdernet_abc'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.map': 'application/json',
}

/* ---------------- SigV4 签名（GET Object，空 query、空 body） ---------------- */
const REGION = 'us-east-1'
const SERVICE = 's3'
const EMPTY_SHA256 = crypto.createHash('sha256').update('').digest('hex')

function sha256hex(s) {
  return crypto.createHash('sha256').update(s).digest('hex')
}
function hmac(key, s) {
  return crypto.createHmac('sha256', key).update(s).digest()
}
/** 生成 ISO 基本格式时间戳：YYYYMMDDTHHMMSSZ */
function amzDateOf(date) {
  return date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')
}
/**
 * 把路径规范化为「严格编码」形式：先 decode 再按段 encodeURIComponent 并补上它不编码的 !'()*。
 * MinIO 计算 canonical URI 时会对路径做规范化（如把字面 '(' 变成 %28），
 * 若我们签名用的路径与它规范化后的不一致就会 SignatureDoesNotMatch。
 * 统一成严格编码后，「线上路径 == 签名 canonical == MinIO canonical」，三者一致。
 */
function strictEncodePath(p) {
  return p
    .split('/')
    .map((seg) => {
      let dec = seg
      try {
        dec = decodeURIComponent(seg)
      } catch {
        /* 保持原样 */
      }
      return encodeURIComponent(dec).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase())
    })
    .join('/')
}
/** 计算 SigV4 Authorization 头 */
function buildAuth({ host, pathname, now }) {
  const amzDate = amzDateOf(now)
  const dateStamp = amzDate.slice(0, 8)
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${EMPTY_SHA256}\nx-amz-date:${amzDate}\n`
  const signedHeaders = 'host;x-amz-content-sha256;x-amz-date'
  const canonicalRequest = ['GET', pathname, '', canonicalHeaders, signedHeaders, EMPTY_SHA256].join('\n')
  const scope = `${dateStamp}/${REGION}/${SERVICE}/aws4_request`
  const stringToSign = ['AWS4-HMAC-SHA256', amzDate, scope, sha256hex(canonicalRequest)].join('\n')
  const kDate = hmac(`AWS4${MINIO_SECRET_KEY}`, dateStamp)
  const kRegion = hmac(kDate, REGION)
  const kService = hmac(kRegion, SERVICE)
  const kSigning = hmac(kService, 'aws4_request')
  const signature = crypto.createHmac('sha256', kSigning).update(stringToSign).digest('hex')
  return {
    authorization: `AWS4-HMAC-SHA256 Credential=${MINIO_ACCESS_KEY}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    amzDate,
  }
}
/** 以服务端时钟（+offsetMs 校正）签名并直接向 MinIO 发起 GET，返回上游响应 */
function signedGet(pathname, offsetMs) {
  return new Promise((resolve, reject) => {
    const target = new URL(MINIO_ENDPOINT)
    const host = target.host
    const { authorization, amzDate } = buildAuth({ host, pathname, now: new Date(Date.now() + offsetMs) })
    const req = http.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === 'https:' ? 443 : 80),
        method: 'GET',
        path: pathname,
        headers: {
          host,
          'x-amz-date': amzDate,
          'x-amz-content-sha256': EMPTY_SHA256,
          authorization,
        },
      },
      resolve,
    )
    req.on('error', reject)
    req.end()
  })
}
function readBody(res) {
  return new Promise((resolve) => {
    const chunks = []
    res.on('data', (c) => chunks.push(c))
    res.on('end', () => resolve(Buffer.concat(chunks)))
    res.on('error', () => resolve(Buffer.alloc(0)))
  })
}
function pipeUpstream(upstream, res) {
  res.writeHead(upstream.statusCode || 502, {
    ...upstream.headers,
    'Access-Control-Allow-Origin': '*',
  })
  upstream.pipe(res)
}

/** 把 /<bucket>/... 请求签名后转发 MinIO；遇时钟偏差自动校正重试一次 */
async function proxyToMinio(req, res) {
  const pathname = strictEncodePath((req.url || '/').split('?')[0])
  if (!MINIO_ACCESS_KEY || !MINIO_SECRET_KEY) {
    res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('server.mjs 未配置 MinIO 密钥（MINIO_ACCESS_KEY / MINIO_SECRET_KEY）')
    return
  }
  let upstream
  try {
    upstream = await signedGet(pathname, 0)
  } catch (e) {
    res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end(`MinIO 连接失败：${e.message}`)
    return
  }
  // 时钟偏差：读 MinIO 的 Date 头算出偏移，用校正后的时间重签一次
  if (upstream.statusCode === 403) {
    const body = await readBody(upstream)
    if (/RequestTimeTooSkewed/.test(body.toString('utf8'))) {
      const serverTime = Date.parse(upstream.headers.date || '')
      const offset = Number.isNaN(serverTime) ? 0 : serverTime - Date.now()
      try {
        upstream = await signedGet(pathname, offset)
      } catch (e) {
        res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end(`MinIO 连接失败：${e.message}`)
        return
      }
      pipeUpstream(upstream, res)
      return
    }
    res.writeHead(403, { ...upstream.headers, 'Access-Control-Allow-Origin': '*' })
    res.end(body)
    return
  }
  pipeUpstream(upstream, res)
}

/* ---------------- 静态文件服务 ---------------- */
/** 静态文件服务；找不到时回退 index.html（hash 路由 SPA） */
function sendStatic(res, urlPath) {
  const root = path.resolve(DIST)
  let filePath = path.join(root, decodeURIComponent(urlPath))
  // 防目录穿越
  if (!path.resolve(filePath).startsWith(root)) {
    res.writeHead(403).end('forbidden')
    return
  }
  let st = existsSync(filePath) ? statSync(filePath) : null
  if (st && st.isDirectory()) {
    filePath = path.join(filePath, 'index.html')
    st = existsSync(filePath) ? statSync(filePath) : null
  }
  if (!st) {
    filePath = path.join(root, 'index.html')
    st = existsSync(filePath) ? statSync(filePath) : null
  }
  if (!st) {
    res.writeHead(404).end('not found')
    return
  }
  const ext = path.extname(filePath).toLowerCase()
  res.writeHead(200, {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': st.size,
  })
  createReadStream(filePath).pipe(res)
}

const server = http.createServer((req, res) => {
  const urlPath = (req.url || '/').split('?')[0]
  const isMinioRoute = urlPath === `/${BUCKET}` || urlPath.startsWith(`/${BUCKET}/`)
  if (isMinioRoute && req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,HEAD,OPTIONS',
      'Access-Control-Allow-Headers': '*',
    })
    res.end()
    return
  }
  if (isMinioRoute) {
    proxyToMinio(req, res)
  } else {
    sendStatic(res, urlPath)
  }
})

server.listen(PORT, () => {
  console.log(`[server] 静态目录: ${DIST}`)
  console.log(`[server] 监听端口: ${PORT}`)
  console.log(`[server] 代理 /${BUCKET}/* -> ${MINIO_ENDPOINT}（服务端 SigV4 签名，时钟自动校正）`)
})
