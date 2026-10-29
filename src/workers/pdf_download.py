#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_download.py —— 从 MinIO 目录批量下载 PDF（零依赖，仅用标准库）

原理：
  与 server.mjs 相同，直接用 SigV4 签名访问 MinIO 的 ListObjectsV2 与 GET Object
  接口（path-style），无需安装 boto3 / minio 等第三方库。连接参数读取项目根目录
  .env / .env.development 中的 MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY。

用法：
  python pdf_download.py                     # 默认下载成品语料目录下 1000 个 PDF 到 workers/pdf
  python pdf_download.py -n 50               # 只下载 50 个
  python pdf_download.py -o D:\\pdfs         # 自定义输出目录
  python pdf_download.py --prefix drivedernet_abc/自筹/xxx/  # 自定义目录（<bucket>/<目录前缀>）
  python pdf_download.py -j 4                # 4 线程并发下载（默认串行）
  python pdf_download.py --force             # 已存在的文件也重新下载（默认跳过，不重复下载）
  python pdf_download.py -q                  # 静默模式，不输出单个文件进度条

说明：
  - 目标目录只列一层（delimiter='/'），筛出 .pdf 后按名称排序取前 N 个。
  - 下载先写入 .part 临时文件，成功后改名，中断不会留下半截 PDF。
  - 单文件失败自动重试（默认 3 次），全部结束后汇总成功/跳过/失败数量。
  - 本地时钟与 MinIO 偏差导致的 RequestTimeTooSkewed 会按响应 Date 头自动校正重签。
"""

import argparse
import hashlib
import hmac
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))          # src/workers
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))     # 项目根目录
DEFAULT_PREFIX = "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料"
REGION = "us-east-1"
SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CHUNK = 1024 * 1024  # 下载块大小（1MB）


# ---------------- 配置读取（.env / 环境变量） ----------------

def load_env_file(path: str) -> dict:
    """极简 .env 解析（KEY = VALUE，忽略注释/空行/包裹引号），与 server.mjs 同规则。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def load_minio_config() -> tuple:
    """读取 MinIO 配置：环境变量 > .env.development > .env。返回 (endpoint, ak, sk)。"""
    env = {}
    for name in (".env", ".env.development", ".env.production"):
        env.update(load_env_file(os.path.join(_PROJECT_ROOT, name)))
    endpoint = (os.environ.get("MINIO_ENDPOINT")
                or env.get("MINIO_ENDPOINT") or env.get("VITE_MINIO_ENDPOINT") or "")
    ak = (os.environ.get("MINIO_ACCESS_KEY")
          or env.get("MINIO_ACCESS_KEY") or env.get("VITE_MINIO_ACCESS_KEY") or "")
    sk = (os.environ.get("MINIO_SECRET_KEY")
          or env.get("MINIO_SECRET_KEY") or env.get("VITE_MINIO_SECRET_KEY") or "")
    if not endpoint or not ak or not sk:
        sys.exit("错误：未能从 .env 或环境变量读取 MinIO 配置（MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY）")
    return endpoint.rstrip("/"), ak, sk


# ---------------- SigV4 签名（与 server.mjs 一致：host + x-amz-date + 空 sha256） ----------------

def _strict_encode(seg: str) -> str:
    """单段严格编码（RFC3986：encodeURIComponent 再补 !'()*），与签名 canonical 保持一致。"""
    return urllib.parse.quote(seg, safe="-_.~")


def strict_encode_path(p: str) -> str:
    """路径逐段规范化：先 decode 再严格编码，保证「线上路径 == canonical URI」。"""
    return "/".join(_strict_encode(seg) for seg in p.split("/"))


def _amz_date(dt: "datetime") -> str:  # noqa: F821
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _canonical_query(params: dict) -> str:
    items = sorted((_strict_encode(k), _strict_encode(v)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def build_auth(endpoint: str, access_key: str, secret_key: str,
               pathname: str, canonical_query: str, now: "datetime") -> dict:  # noqa: F821
    """计算 SigV4 Authorization 头（GET、空 body）。"""
    host = urllib.parse.urlparse(endpoint).netloc
    amz_date = _amz_date(now)
    date_stamp = amz_date[:8]
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{EMPTY_SHA256}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["GET", pathname, canonical_query, canonical_headers, signed_headers, EMPTY_SHA256])
    scope = f"{date_stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])

    def hmac_bytes(key, s):
        return hmac.new(key, s.encode(), hashlib.sha256).digest()

    signing = hmac_bytes(hmac_bytes(hmac_bytes(hmac_bytes(
        f"AWS4{secret_key}".encode(), date_stamp), REGION), SERVICE), "aws4_request")
    signature = hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": EMPTY_SHA256,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }


def signed_request(endpoint: str, ak: str, sk: str, pathname: str, params: dict,
                   offset_sec: int = 0, stream: bool = False):
    """签名并发起 GET；返回响应对象。stream=True 时调用方负责读取/关闭。"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_sec)
    query = _canonical_query(params)
    headers = build_auth(endpoint, ak, sk, pathname, query, now)
    url = endpoint + pathname + ("?" + query if query else "")
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=60)


def skew_offset(err_body: bytes, err_headers) -> "int | None":
    """若响应体是 RequestTimeTooSkewed，按 MinIO 的 Date 头算出秒级时钟偏差；否则 None。"""
    if b"RequestTimeTooSkewed" not in err_body:
        return None
    from email.utils import parsedate_to_datetime
    date_str = err_headers.get("Date") if err_headers else None
    if not date_str:
        return None
    try:
        import datetime
        server = parsedate_to_datetime(date_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        return int((server - now).total_seconds())
    except Exception:
        return None


# ---------------- 列目录（ListObjectsV2，只列一层） ----------------

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def list_pdfs(endpoint: str, ak: str, sk: str, prefix: str, quiet: bool = False,
              max_pdf: int = 0) -> list:
    """列 prefix 目录（delimiter='/'）下的 .pdf 对象，返回 <bucket>/<key> 完整路径列表，按路径排序。

    自动翻页；max_pdf > 0 时收集够就提前停（目录可能几十万个对象，全量翻页会很久没输出），
    结果不足 max_pdf 则说明已列完。
    """
    bucket, _, key_prefix = prefix.partition("/")
    if not bucket or not key_prefix:
        sys.exit(f"错误：--prefix 需为 <bucket>/<目录前缀>，当前：{prefix!r}")
    if not key_prefix.endswith("/"):
        key_prefix += "/"
    if not quiet:
        print(f"正在列目录：{prefix} …（大目录可能较慢，已限制最多扫 {max_pdf or '不限'} 个）", flush=True)
    pathname = strict_encode_path(f"/{bucket}")
    keys, token, offset, full = [], None, 0, False
    while True:
        params = {"list-type": "2", "encoding-type": "url", "max-keys": "1000",
                  "prefix": key_prefix, "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        try:
            resp = signed_request(endpoint, ak, sk, pathname, params, offset_sec=offset)
            body = resp.read()
        except urllib.error.HTTPError as e:
            err = e.read()
            new_offset = skew_offset(err, e.headers)
            if new_offset is not None and offset == 0:
                offset = new_offset
                continue
            sys.exit(f"错误：列目录失败 HTTP {e.code}：{err.decode('utf-8', 'replace')[:300]}")
        # 响应来自自有 MinIO，但仍禁用 DTD/实体解析，避免实体注入类攻击
        if b"<!DOCTYPE" in body or b"&" in body.replace(b"&amp;", b"").replace(b"&lt;", b"") \
                .replace(b"&gt;", b"").replace(b"&quot;", b"").replace(b"&apos;", b""):
            sys.exit("错误：列目录响应含非法 DTD/实体，已中止")
        root = ET.fromstring(body)
        # encoding-type=url 时 Key 是 URL 编码的，需 unquote
        for c in root.findall(f"{_NS}Contents"):
            key = urllib.parse.unquote(c.findtext(f"{_NS}Key", ""))
            if key.lower().endswith(".pdf"):
                # 响应里的 Key 是桶内相对路径，补回桶名前缀，后续直接拼 URL 路径
                keys.append(f"{bucket}/{key}")
        if root.findtext(f"{_NS}IsTruncated", "false").lower() != "true":
            full = True
            break
        if max_pdf > 0 and len(keys) >= max_pdf:
            break
        token = urllib.parse.unquote(root.findtext(f"{_NS}NextContinuationToken", ""))
        if not quiet:
            print(f"列目录中：已找到 {len(keys)} 个 PDF…", end="\r", flush=True)
    keys.sort()
    if not quiet:
        print(" " * 60, end="\r") if not full else None
        total_msg = f"目录共 {len(keys)} 个 PDF（不含子目录）" if full \
            else f"已列到 {len(keys)} 个 PDF（目标目录对象很多，按需提前截断）"
        print(total_msg)
    return keys


# ---------------- 进度条（风格同 pdf_inflate.py，TTY 下原地刷新，重定向自动静音） ----------------

def human_size(num) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}TB"


def human_secs(sec: float) -> str:
    """时长格式化：亚秒显毫秒，避免小文件不足 0.5s 被取整成「用 0s」。"""
    sec = max(float(sec), 0.0)
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    return f"{int(m)}m{s:04.1f}s"


def _disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if _disp_width(text) <= limit:
        return text
    out, w = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > limit - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


class Progress:
    """单文件下载进度条：[名] |██░░| 12.3MB/45.6MB 27.0% 3.1MB/s 用 4s。非 TTY 自动静音。"""

    _REFRESH = 0.1

    def __init__(self, label: str, total: int, quiet: bool, cols: int, lock: threading.Lock):
        self.stream = sys.stderr
        self.label = _truncate(label, max(cols // 4, 10))
        self.total = max(int(total), 0)
        self.done = 0
        self.t0 = time.time()
        self._last = 0.0
        self._last_w = 0
        self._cols = cols
        self._lock = lock
        self.enabled = (not quiet) and hasattr(self.stream, "isatty") and self.stream.isatty()

    def update(self, nbytes: int):
        self.done += nbytes
        now = time.time()
        if not self.enabled or (now - self._last < self._REFRESH and self.done < self.total):
            return
        self._last = now
        elapsed = max(now - self.t0, 1e-6)
        ratio = min(self.done / self.total, 1.0) if self.total > 0 else 0.0
        rate = self.done / elapsed
        bar_w = min(max(self._cols - _disp_width(self.label) - 46, 10), 30)
        filled = int(bar_w * ratio)
        line = (f"\r[{self.label}] |{'█' * filled}{'░' * (bar_w - filled)}| "
                f"{human_size(self.done)}/{human_size(self.total)} {ratio * 100:5.1f}% "
                f"{human_size(rate)}/s 用 {human_secs(elapsed)}")
        line = _truncate(line, self._cols)
        with self._lock:
            try:
                self.stream.write(line + " " * max(self._last_w - _disp_width(line), 0))
            except UnicodeEncodeError:
                self.stream.write(line.encode("ascii", "ignore").decode("ascii"))
            self.stream.flush()
            self._last_w = _disp_width(line)

    def close(self):
        if not self.enabled:
            return
        self.update(0)
        with self._lock:
            self.stream.write("\n")
            self.stream.flush()
            self._last_w = 0


# ---------------- 下载 ----------------

_print_lock = threading.Lock()


def log(msg: str):
    with _print_lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # 控制台编码（如 GBK）编不出特殊字符时降级输出，不让打印炸掉整个下载
            print(msg.encode(sys.stdout.encoding or "utf-8", "replace").decode(
                sys.stdout.encoding or "utf-8", "replace"), flush=True)


def download_one(endpoint: str, ak: str, sk: str, key: str, out_dir: str,
                 idx: int, total: int, quiet: bool, cols: int, lock: threading.Lock,
                 retries: int = 3, mark_ok: str = "✓", mark_fail: str = "✗") -> tuple:
    """下载单个对象到 out_dir（跳过已存在）。返回 (结果, 字节数)：结果 'ok' / 'skip' / 'fail'。"""
    fname = os.path.basename(key)
    dst = os.path.join(out_dir, fname)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return "skip", 0
    pathname = strict_encode_path("/" + key)
    offset, last_err = 0, ""
    for attempt in range(1, retries + 1):
        prog = Progress(fname, 0, quiet, cols, lock)
        tmp = dst + ".part"
        try:
            resp = signed_request(endpoint, ak, sk, pathname, {}, offset_sec=offset, stream=True)
            length = int(resp.headers.get("Content-Length") or 0)
            prog.total = length
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    prog.update(len(chunk))
            if length and done != length:
                raise OSError(f"下载不完整（{done}/{length} 字节）")
            os.replace(tmp, dst)
            prog.close()
            log(f"[{idx}/{total}] {mark_ok} {fname}（{human_size(done)}，{human_secs(time.time() - prog.t0)}）")
            return "ok", done
        except urllib.error.HTTPError as e:
            body = e.read()
            prog.close()
            new_offset = skew_offset(body, e.headers)
            if new_offset is not None and offset == 0:
                offset = new_offset
                continue
            last_err = f"HTTP {e.code}"
        except Exception as e:  # 网络中断/写盘失败等
            prog.close()
            last_err = str(e)
        if attempt < retries:
            time.sleep(min(attempt * 2, 5))
    os.remove(tmp) if os.path.isfile(dst + ".part") else None
    log(f"[{idx}/{total}] {mark_fail} {fname} 失败：{last_err}（重试 {retries} 次）")
    return "fail", 0


def main():
    parser = argparse.ArgumentParser(
        description="从 MinIO 目录批量下载 PDF（跳过已下载，带进度显示）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="目录前缀（<bucket>/<路径>）")
    parser.add_argument("-n", "--count", type=int, default=1000, help="最多下载个数")
    parser.add_argument("-o", "--out", default=os.path.join(_SCRIPT_DIR, "pdf"), help="输出目录")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="并发下载线程数")
    parser.add_argument("--retries", type=int, default=3, help="单文件失败重试次数")
    parser.add_argument("--force", action="store_true", help="已存在的文件也重新下载")
    parser.add_argument("--list-cap", type=int, default=0,
                        help="列目录时最多收集多少个 PDF 就提前停（0=自动：下载数×3，避免几十万对象的全量翻页）")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默模式，不输出单文件进度条")
    args = parser.parse_args()

    endpoint, ak, sk = load_minio_config()
    cap = args.list_cap if args.list_cap > 0 else (0 if args.force else max(args.count * 3, 100))
    keys = list_pdfs(endpoint, ak, sk, args.prefix, quiet=args.quiet, max_pdf=cap)
    if not keys:
        sys.exit("错误：目录下没有找到 PDF")

    skipped = 0
    if args.force:
        targets = keys
    else:
        os.makedirs(args.out, exist_ok=True)
        targets = [k for k in keys
                   if not (lambda p: os.path.isfile(p) and os.path.getsize(p) > 0)(
                       os.path.join(args.out, os.path.basename(k)))]
        skipped = len(keys) - len(targets)
    capped = len(targets) > args.count
    targets = targets[:args.count]
    if not targets:
        print("无需下载（待下载数为 0，或 -n 为 0）" if args.count else
              f"列表检查：收集到 {len(keys)} 个，其中 {len(targets)} 个待下载（-n 0 不执行下载）")
        return
    if not args.quiet:
        parts = []
        if skipped:
            parts.append(f"已存在跳过 {skipped} 个")
        if capped:
            parts.append(f"列表内新文件多于上限，只下载前 {args.count} 个（-n 可调）")
        print("，".join(parts) if parts else f"待下载 {len(targets)} 个")
        print(f"本次将下载 {len(targets)} 个", flush=True)

    os.makedirs(args.out, exist_ok=True)
    cols = shutil.get_terminal_size((100, 24)).columns
    # ✓/✗ 在 GBK 等控制台编码下可能编不出，编码不了就降级为 [OK]/[X]
    try:
        "✓✗".encode(sys.stdout.encoding or "utf-8")
        mark_ok, mark_fail = "✓", "✗"
    except UnicodeEncodeError:
        mark_ok, mark_fail = "[OK]", "[X]"
    lock = threading.Lock()
    total = len(targets)
    start = time.time()
    counts = {"ok": 0, "skip": 0, "fail": 0}
    done_cnt = 0

    def worker(item):
        nonlocal done_cnt
        idx, key = item
        result, _ = download_one(endpoint, ak, sk, key, args.out, idx, total,
                                 args.quiet, cols, lock, args.retries, mark_ok, mark_fail)
        with lock:
            done_cnt += 1
            counts[result] += 1
        return result

    items = list(enumerate(targets, 1))
    if args.jobs > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(worker, items))
    else:
        for item in items:
            worker(item)

    elapsed_all = time.time() - start
    avg = f"，均速 {elapsed_all / total:.2f}s/个" if total else ""
    print(f"完成：成功 {counts['ok']}，跳过 {counts['skip']}，失败 {counts['fail']}"
          f"，总用时 {human_secs(elapsed_all)}{avg}，输出目录：{os.path.abspath(args.out)}")
    if counts["fail"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
