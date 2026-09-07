/**
 * 生产环境一体化服务器：静态文件 + MinIO 反向代理（零依赖，Node 内置模块）。
 *
 * 用来替代 `http-server -p 3344 -P http://...`：http-server 的 -P 代理行为不可控
 * （可能改动 Host / 路径），会破坏 S3 SigV4 签名。本脚本显式保证：
 *   - 转发到 MinIO 时 Host 头保持为「浏览器请求的 host」（SDK 就是按它签名的）
 *   - 路径 + query 原样透传，不重写、不归一化
 * 从而让 SigV4 签名在 MinIO 侧校验通过。
 *
 * 用法（在 dist 目录下，即含 index.html/assets 的目录）：
 *   node server.mjs
 * 可用环境变量覆盖：PORT / DIST / MINIO_TARGET / MINIO_BUCKET
 */
import http from 'node:http'
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

// 默认服务当前工作目录（即你运行 node server.mjs 时所在的 dist 目录）
const DIST = path.resolve(process.env.DIST || process.cwd())

/** 极简 .env 解析（KEY = VALUE，忽略注释/空行/包裹引号），让 server.mjs 与前端共用一份配置 */
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
// 读取运行目录下的 .env / .env.production（build 时已拷进 dist）；优先级低于真实环境变量
const fileEnv = {
  ...loadEnvFile(path.resolve(DIST, '.env')),
  ...loadEnvFile(path.resolve(DIST, '.env.production')),
}

// 监听端口：环境变量 PORT > .env 文件 SERVER_PORT > 默认 3344
const PORT = Number(process.env.PORT || fileEnv.SERVER_PORT || 3344)
// MinIO 地址/桶：环境变量 > .env 文件 > 硬编码默认
const MINIO_TARGET = process.env.MINIO_TARGET || fileEnv.VITE_MINIO_ENDPOINT || 'http://10.89.14.234:12001'
const BUCKET = process.env.MINIO_BUCKET || fileEnv.VITE_MINIO_BUCKET || 'drivdernet_abc'

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

/** 反代到 MinIO：保留 Host、原样透传 path+query，保证 SigV4 校验通过 */
function proxyToMinio(req, res) {
  const target = new URL(MINIO_TARGET)
  const headers = { ...req.headers }
  // 关键：Host 保持为浏览器请求的 host（SDK 按它签名），不要换成 MinIO 的地址
  headers.host = req.headers.host
  const proxyReq = http.request(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      method: req.method,
      path: req.url, // 原样（含 query），不重写
      headers,
    },
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 502, proxyRes.headers)
      proxyRes.pipe(res)
    },
  )
  proxyReq.on('error', (e) => {
    if (!res.headersSent) res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end(`MinIO 代理错误：${e.message}`)
  })
  req.pipe(proxyReq)
}

const server = http.createServer((req, res) => {
  const urlPath = (req.url || '/').split('?')[0]
  if (urlPath === `/${BUCKET}` || urlPath.startsWith(`/${BUCKET}/`)) {
    proxyToMinio(req, res)
  } else {
    sendStatic(res, urlPath)
  }
})

server.listen(PORT, () => {
  console.log(`[server] 静态目录: ${DIST}`)
  console.log(`[server] 监听端口: ${PORT}`)
  console.log(`[server] 代理 /${BUCKET}/* -> ${MINIO_TARGET}（保留 Host，SigV4 透传）`)
})
