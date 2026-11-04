#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_backup_sssss.py —— 把一个目录里的 _sssss 原件备份到本地，并给桶里的膨胀版出一份校验清单

为什么新开一个脚本（不改 pdf_rename_inflate_upload.py）：
  那个脚本是「写桶」的（条件 PUT 建 _sssss、PUT 覆盖 X.pdf），本脚本要的是「只读」。
  把备份逻辑塞进能改桶的脚本里，最坏情况是一个参数打错就把桶动了；本脚本从代码层面
  只有 GET 与 ListObjectsV2 —— 不出现任何 PUT/POST/DELETE，物理上不可能改桶。
  顺带符合本目录惯例：一件事一个零依赖单文件（pdf_download / pdf_check_damaged /
  pdf_inflate / pdf_rename_inflate_upload），本文件同样单独拷到 Linux 就能跑。

它到底备了什么（两样，用途不同）：
  1) 桶里每个 X_sssss.pdf（原件）→ 真下载到本地，逐个算 sha256。
     这是「回滚线索」：万一膨胀版有问题，原件还在，能重做。
  2) 桶里每个 X.pdf（膨胀完成版）→ 只记 大小 + ETag，不下内容。
     这是「没被动过的证据」：备份完、删完之后各核一次，两个数字都对得上，
     就说明你删 _sssss 的过程中膨胀版一个字节都没变。内容不下载是因为
     膨胀版是原件的 100 倍以上，一个目录就要 10TB 级磁盘，不现实。

删除前的硬闭环（这是本脚本存在的意义）：
  列目录 → 记下「桶里 _sssss 全集 S」→ 全部下载并算哈希 → 重新列一次目录得 S' →
  S == S' 且每个本地文件都在、大小一致 → 才输出 deletable_keys.txt 并打印「可以删」。
  数量对不齐 / 有缺失 / 列目录期间桶被别的任务动过 → 明确打印「先别删」。
  所以默认只列一层（和处理脚本口径一致）；要递归删就得 --recursive 递归备。

用法（Linux 用 python3，Windows 用 python）：--endpoint 和 --prefix 都必带，少一个直接报错
      退出（脚本里不留默认目录，也不从 .env 读目录）；目录写 <bucket>/<路径>，开头不带 /：
  python3 pdf_backup_sssss.py --endpoint http://10.0.0.1:9000 \
      --prefix "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料" --dry-run
  python3 pdf_backup_sssss.py --endpoint http://10.0.0.1:9000 \
      --prefix "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料" -j 8
  python3 pdf_backup_sssss.py --endpoint http://10.0.0.1:9000 \
      --prefix "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料" --check
  export MINIO_ENDPOINT=http://10.0.0.1:9000 PDF_PREFIX="drivdernet_abc/自筹/…/成品语料"
  # 后台跑（重定向里问不到人）必须显式 --yes，否则脚本会算完账就退出、不会开下载：
  nohup python3 -u pdf_backup_sssss.py --endpoint http://10.0.0.1:9000 \
      --prefix "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料" \
      -j 8 --yes > backup_5.1通用.out 2>&1 &

Windows（PowerShell）干脆写成一长行，记得给目录加双引号（路径里有中文和下划线都没事，
      但 PowerShell 会把不引起来的中文路径按空白切词）：
  python pdf_backup_sssss.py --endpoint http://10.0.0.1:9000 --prefix "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料" --dry-run

说明：
  - 只读：全程不发 PUT/POST/DELETE；改桶请回去用 pdf_rename_inflate_upload.py。
  - 膨胀版 X.pdf 默认不下：只往清单里记大小+ETag，当「成品没被动过」的凭据。
    想连膨胀版本体一起存到本地就加 --with-inflated：它一个八到十一 MB，比原件大十几倍，
    会和原件一起走同一道容量预检，预检不过直接中止（这种情形连 --yes 也不放行）。
  - 断点续传：先写 .part 再改名，中断不留半截文件；重跑时本地已存在且大小一致就跳过
    （只补算 sha256），--force 才重下。输出目录不带时间戳，就是为了能反复重跑接着备。
  - 清单 manifest.tsv（UTF-8，制表符分隔，和处理脚本日志同风格）：
      kind  key  size  etag  sha256  local  status
    kind = COPY(_sssss 原件) | INFLATED(膨胀版：默认只记 size+etag，--with-inflated 时真下载)。
    status = 已下载 / 已存在跳过 / 待下载(未开始) / 失败:原因 / 清单(不下内容) / 补记(备份时缺)。
    用 --check 复跑会把本地文件的实测大小/哈希再填一遍，方便事后自证。
  - 两份名单文件（同一目录，作用相反）：deletable_keys.txt = 逐条核过、可以删的；
    pending_inflated_keys.txt = 同层还欠一个膨胀版、绝对不能删的（它们是重做膨胀版的
    唯一凭据）。后者每行一个文件名、# 开头是注释，格式正好能喂给处理脚本的名单模式：
        python pdf_rename_inflate_upload.py --prefix "<同一个目录>" --files-from pending_inflated_keys.txt
  - ETag 注意：单次 PUT 的 ETag 就是内容 MD5（我们自己的膨胀版就是这么传的），但分片
    上传的对象 ETag 形如 "xxxx-3"、不是 MD5。所以本脚本只把它当「变没变」的凭据，
    不当「内容对不对」的凭据 —— 内容凭据一律用本地 sha256。
  - 下载前先算账、再点头（这是动手前的最后一道闸）：列完目录先把「本轮总共要备多少 /
    本地已有可跳过多少 / 还要新写多少 / 目标盘剩余多少（按 --min-free 先扣掉预留余量）」
    打印出来；算下来装不下就直接中止（这种情形连 --yes 也不放行，写满盘会让 .part
    写失败、还拖累同一块盘上的其他进程）；算得下也要人工确认才开工：TTY 里问一句
    [y/N]，nohup/重定向这种非交互环境问不到人，就必须显式带 --yes（不能把后台任务
    卡在等输入上）。只想看账不开下载就用 --dry-run（它不问也不写盘）。
  - 目录（--prefix）必填（不带就报错退出，脚本里没有默认目录），而且和 --out 配套：
    不同目录默认落到 backup/<目录最后两段>/ 各自独立的备份文件夹和 manifest.tsv，不会互相覆盖；
    但手动把两个目录指到同一个 --out，后跑的会把先跑的清单整份重写、备份件也挤在一层，
    那两份账就都别信了。--check 会用清单头部记的 prefix 和当前 --prefix 对一下，不一致当场警告。
  - Linux/Windows 兼容：纯标准库，Python 3.6+；文件 LF 行尾可 chmod +x；stdout/stderr
    自动切 UTF-8（errors=replace），LANG=C 容器里打中文不会抛 UnicodeEncodeError；
    对象 key 一律按 '/' 处理；默认扁平存放（同一层不会有重名），--recursive 才镜像子目录。
"""

import argparse
import datetime
import hashlib
import hmac
import io
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
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.version_info < (3, 6):
    sys.exit("需要 Python 3.6 及以上（当前：%s）" % sys.version.split()[0])

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== 配置区：AK/SK 与目标目录（地址走 --endpoint，与控制脚本一致） ====================
CONFIG = {
    # ---- MinIO 连接：地址故意不写死（各机器不同），AK/SK 与处理脚本保持同一份 ----
    "MINIO_ENDPOINT": "",
    "MINIO_ACCESS_KEY": "NXKKBDFEZP9XNNHVRIHT",
    "MINIO_SECRET_KEY": "Fx0k0Ix2Pab4hkZMOvl3oLLKL08W5ZPVmBiTyJUS",
    # ---- 要备份的目录：<bucket>/<目录路径> —— 故意不设默认值 ----
    #      必须命令行 --prefix（或环境变量 PDF_PREFIX）显式给，不给就报错退出：多个目录轮流
    #      处理时，写死的默认值最容易让人拿着上一个目录跑完整轮，盘时白花、清单还落在错的
    #      备份文件夹里。开头不带 /（桶名就是 key 的第一段，和处理脚本保持同一写法）。
    # ---- 本地备份根目录（空=取「脚本同目录/backup/<目录尾段>」，反复跑接着备） ----
    "OUT_DIR": "",
    # ---- 要备份的对象后缀：现行产物是 _sssss，老版本遗留 _smallsmall/_s ----
    #      默认只备现行 _sssss；想连历史遗留一起备就 --suffix "_sssss,_smallsmall,_s" ----
    "SUFFIX": "_sssss",
    "AWS_REGION": "us-east-1",
    "AWS_SERVICE": "s3",
    "JOBS": 4,            # 并发下载线程数
    "RETRIES": 4,
    "TIMEOUT": 180,
}

REGION = CONFIG["AWS_REGION"]
SERVICE = CONFIG["AWS_SERVICE"]
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TIMEOUT = int(CONFIG["TIMEOUT"])
_MIB = 1024 * 1024
_CHUNK = 4 * 1024 * 1024      # 下载块（4MB，磁盘顺序写比这慢不了多少，系统调用少）
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

try:
    _HEARTBEAT = max(1.0, float(os.environ.get("PDF_HEARTBEAT") or 5.0))
except ValueError:
    _HEARTBEAT = 5.0


# ---------------- 配置读取（命令行 > 环境变量 > CONFIG > .env） ----------------

def load_env_file(path: str) -> dict:
    """极简 .env 解析（KEY = VALUE，忽略注释/空行/包裹引号）；文件不存在返回空表。"""
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


_DOTENV_CACHE = None


def _dotenv_values() -> dict:
    """收集本机可能存在的 .env（脚本同目录 + 仓库内的项目根），仅作兜底，缺失不报错。"""
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE
    env = {}
    roots = [_SCRIPT_DIR, os.path.dirname(os.path.dirname(_SCRIPT_DIR))]
    seen = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        for name in (".env", ".env.development", ".env.production"):
            env.update(load_env_file(os.path.join(root, name)))
    _DOTENV_CACHE = env
    return env


def _pick(name: str, cli_value=None, env_name=None, dotenv_keys=None) -> str:
    for v in (cli_value, os.environ.get(env_name or name), CONFIG.get(name)):
        if v not in (None, ""):
            return str(v).strip()
    dot = _dotenv_values()
    for k in (dotenv_keys or (name, "VITE_" + name)):
        if dot.get(k):
            return str(dot[k]).strip()
    return ""


_ENDPOINT_SRC = ""
_PREFIX_SRC = ""


def resolve_conn(args) -> tuple:
    """返回 (endpoint, ak, sk)。地址不写死，缺了就给可操作报错（与处理脚本同一套口径）。"""
    global _ENDPOINT_SRC
    ep = (args.endpoint or os.environ.get("MINIO_ENDPOINT") or "").strip()
    src = "命令行 --endpoint" if args.endpoint else ("环境变量 MINIO_ENDPOINT" if ep else "")
    if not ep:
        dot = _dotenv_values()
        for k in ("MINIO_ENDPOINT", "VITE_MINIO_ENDPOINT"):
            if dot.get(k):
                ep = str(dot[k]).strip()
                src = ".env 的 %s" % k
                break
    ak = _pick("MINIO_ACCESS_KEY", args.access_key)
    sk = _pick("MINIO_SECRET_KEY", args.secret_key)
    if not ep:
        sys.exit("错误：没有 MinIO 地址。请用 --endpoint 传入，或设环境变量 MINIO_ENDPOINT\n"
                 "  例：--endpoint http://10.0.0.1:9000\n"
                 "  例：export MINIO_ENDPOINT=http://10.0.0.1:9000   "
                 "（Windows：set MINIO_ENDPOINT=http://10.0.0.1:9000）")
    if not ak or not sk:
        sys.exit("错误：MinIO 凭据不完整（当前 ak=%s,sk=%s）。用 --access-key/--secret-key "
                 "或环境变量传入" % (bool(ak), bool(sk)))
    ep = ep.rstrip("/")
    if "://" not in ep:
        ep = "http://" + ep
    _ENDPOINT_SRC = src
    return ep, ak, sk


def resolve_prefix(args) -> tuple:
    """返回 (prefix, bucket, key_prefix)。目录必须显式给：--prefix 或环境变量 PDF_PREFIX
       二选一，脚本里没有默认目录，也不读 .env —— 多个目录轮流处理时，写死的默认值最容易
       让人拿着上一个目录跑完整轮（盘时白花、清单记在错的文件夹里，整轮作废）。
       写法：<bucket>/<目录路径>，开头不带 /。带了会顺手 lstrip 掉，但请别带：S3 语义里
       前导 / 不是分隔符而是 key 的一部分，MinIO 真有一个以 / 开头的 key 时，加上这一划
       就把它们全漏掉了（表现为「这目录是空的」）。结尾也不必备 /，列目录时会补。"""
    global _PREFIX_SRC
    if args.prefix:
        raw, _PREFIX_SRC = args.prefix, "命令行 --prefix"
    elif os.environ.get("PDF_PREFIX"):
        raw, _PREFIX_SRC = os.environ["PDF_PREFIX"], "环境变量 PDF_PREFIX"
    else:
        raw, _PREFIX_SRC = "", ""
    prefix = str(raw).strip().lstrip("/")
    if not prefix:
        sys.exit("错误：必须指定要备份的目录（本脚本不留默认目录，--prefix 必填）。\n"
                 "  例：--prefix \"drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料\"\n"
                 "  后台包装脚本也可用环境变量 PDF_PREFIX=\"<bucket>/<目录>\"；\n"
                 "  写法是 <bucket>/<目录路径>：开头不要带 /，PowerShell 里记得加引号；\n"
                 "  默认只列这一层下的对象，要连子目录一起备再加 --recursive；\n"
                 "  忘了完整路径就先到处理脚本 pdf_rename_inflate_upload.py 的 CONFIG['PREFIX']\n"
                 "  或处理日志文件名（尾两段就是目录尾）里抄。")
    if "/" not in prefix:
        sys.exit("错误：--prefix 需为 <bucket>/<目录路径>（至少桶名加一层目录），当前：%r" % raw)
    bucket, _, key_prefix = prefix.partition("/")
    return prefix, bucket, key_prefix


# ---------------- 终端编码、路径与签名工具（与处理脚本同源，保证同一套签名口径） ----------------

def _stream_needs_utf8(stream) -> bool:
    enc = (getattr(stream, "encoding", "") or "").lower()
    if not enc:
        return True
    try:
        "中✓".encode(enc)
        return False
    except (LookupError, UnicodeEncodeError):
        return True


def ensure_utf8_streams():
    """能在 GBK 控制台编中文就别强改 UTF-8（那会乱码），只把错误处理设成 replace；
       LANG=C/POSIX 的容器编不出中文才切 UTF-8。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        switch = _stream_needs_utf8(stream)
        try:
            if switch:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
            continue
        except Exception:
            pass
        if not switch:
            continue
        try:
            wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
        except Exception:
            continue
        setattr(sys, name, wrapped)


def _strict_encode(seg: str) -> str:
    return urllib.parse.quote(seg, safe="-_.~")


def strict_encode_path(p: str) -> str:
    return "/".join(_strict_encode(seg) for seg in p.split("/"))


def _amz_date(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _canonical_query(params: dict) -> str:
    items = sorted((_strict_encode(k), _strict_encode(v)) for k, v in params.items())
    return "&".join("%s=%s" % (k, v) for k, v in items)


def skew_offset(err_body: bytes, err_headers):
    """响应体是 RequestTimeTooSkewed 时按 MinIO 的 Date 头算秒级偏差；否则 None。"""
    if b"RequestTimeTooSkewed" not in err_body:
        return None
    from email.utils import parsedate_to_datetime
    date_str = err_headers.get("Date") if err_headers else None
    if not date_str:
        return None
    try:
        server = parsedate_to_datetime(date_str)
        return int((server - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except Exception:
        return None


def human_size(num) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return "%d%s" % (num, unit) if unit == "B" else "%.1f%s" % (num, unit)
        num /= 1024.0
    return "%.1fTB" % num


def human_secs(sec: float) -> str:
    sec = max(float(sec), 0.0)
    if sec < 1:
        return "%.0fms" % (sec * 1000)
    if sec < 60:
        return "%.1fs" % sec
    m, s = divmod(sec, 60)
    return "%dm%04.1fs" % (int(m), s)


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


_SAFE_BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]')


def safe_name(name: str) -> str:
    """对象名落到本地文件系统：Windows 保留字符与控制字符换下划线，两端结果一致。"""
    out = _SAFE_BAD.sub("_", name)
    # Windows 末尾的点和空格会被悄悄吃掉，本地名和清单就对不上了，这里提前顶掉
    return out.rstrip(". ") or "_"


class Progress:
    """单文件下载进度条；非 TTY（nohup/重定向）自动静音，由心跳接管。"""

    _REFRESH = 0.1

    def __init__(self, label: str, total: int, quiet: bool, cols: int, lock):
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
        line = ("\r[%s] |%s%s| %s/%s %5.1f%% %s/s 用 %s" % (
            self.label, "█" * filled, "░" * (bar_w - filled),
            human_size(self.done), human_size(self.total), ratio * 100,
            human_size(rate), human_secs(elapsed)))
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


_lock = threading.Lock()
_tally_lock = threading.Lock()
_hb = {"last": 0.0}


def hb_reset():
    """每轮列目录前归零，不然第二轮会被上一轮的时间戳挡住不开口。"""
    _hb["last"] = 0.0


def hb_line(label: str, quiet: bool, done: int, total: int, t0: float, note: str = ""):
    """静默/后台模式下每 _HEARTBEAT 秒报一行整行进度（重定向文件里 \\r 会糊成一坨）。
       done==total（收尾那一行）不限流，必打，不然最后只看到一个中间数。"""
    if not quiet:
        return
    now = time.time()
    if done != total and now - _hb["last"] < _HEARTBEAT:
        return
    _hb["last"] = now
    line = "[%s] %s %d/%d %s" % (human_secs(now - t0), label, done, total, note)
    with _lock:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def hb_scan(quiet, scanned, n_copy, pages, t0, suffixes, force=False):
    """列目录这种「拿不到总数」的长跑专用心跳：前台（交互终端）用 \\r 原地刷一行，
       静默/重定向时每 _HEARTBEAT 秒报整行。
       hb_line 是 done/total 语义、列目录根没有 total 可给；而它开头那句「not quiet 就静音」
       正是大目录前台跑时屏幕一片空白、看着像卡死的原因 —— 几十万个对象要翻几百页。"""
    now = time.time()
    if not force and now - _hb["last"] < _HEARTBEAT:
        return
    _hb["last"] = now
    if force:
        if not quiet:
            with _lock:
                sys.stderr.write("\n")
                sys.stderr.flush()
        return
    rate = scanned / (now - t0) if now > t0 else 0
    line = ("[%s] 列目录中… 已扫 %s 个对象（其中要备的 %s %s 个），翻了 %s 页，约 %s 个/秒"
            % (human_secs(now - t0), format(int(scanned), ","),
               "+".join(suffixes or ()) or "-", format(int(n_copy), ","),
               format(int(pages), ","), format(int(rate), ",")))
    cut = _truncate(line, 100)
    with _lock:
        if quiet:
            sys.stderr.write(cut + "\n")
        else:
            sys.stderr.write("\r" + cut + " " * max(0, 100 - _disp_width(cut)))
        sys.stderr.flush()


# ---------------- 列目录（ListObjectsV2）：只读，一次拿回 key + size + ETag ----------------

def list_objects(endpoint, ak, sk, bucket, key_prefix, quiet=False, recursive=False,
                 max_list=0, copy_suffixes=None, stats=None):
    """翻遍 prefix，返回 [(key, size, etag)]。

    stats：可选的一个 dict，用来把「扫到哪了」的计数实时透给调用方——人 Ctrl+C 的时候，
    函数内部的局部变量跟着栈一起没了，不递出来就没人知道他到底扫了多少。

    为什么用列目录而不是逐个 HEAD：ListObjectsV2 的每一行本来就带 Size 和 ETag，
    所以「有哪些 _sssss、各多大」和「对应膨胀版多大、ETag 是多少」一次翻页就全拿到了，
    十几万个对象省掉两倍请求。
    默认 delimiter='/'（只列一层），和 pdf_rename_inflate_upload.py 的处理口径完全一致：
    处理脚本只动这一层，备份这一层就够；要递归删就得加 --recursive 递归备。
    """
    if not key_prefix.endswith("/"):
        key_prefix += "/"
    hb_reset()
    pathname = strict_encode_path("/" + bucket)
    out, token, offset, complete = [], None, 0, False
    n_copy = 0            # 已列对象里有多少是待备份的 _sssss（长跑时唯一能判断「列对了」的信号）
    t0 = time.time()
    scanned = 0
    pages = 0
    while True:
        pages += 1
        params = {"list-type": "2", "encoding-type": "url", "max-keys": "1000",
                  "prefix": key_prefix}
        if not recursive:
            params["delimiter"] = "/"
        if token:
            params["continuation-token"] = token
        try:
            resp = _signed_get(endpoint, ak, sk, pathname, params, offset_sec=offset,
                               fatal_conn=True)
            body = resp.read()
            resp.close()
        except urllib.error.HTTPError as e:
            err = b""
            try:
                err = e.read()
            except Exception:
                pass
            new_offset = skew_offset(err, e.headers)
            if new_offset is not None and offset == 0:
                offset = new_offset
                continue
            sys.exit("错误：列目录失败 HTTP %s：%s" % (
                e.code, err.decode("utf-8", "replace")[:300]))
        # 响应来自自有 MinIO，但仍禁 DTD/实体解析，避免实体注入类攻击
        if b"<!DOCTYPE" in body or b"&" in body.replace(b"&amp;", b"").replace(b"&lt;", b"") \
                .replace(b"&gt;", b"").replace(b"&quot;", b"").replace(b"&apos;", b""):
            sys.exit("错误：列目录响应含非法 DTD/实体，已中止")
        root = ET.fromstring(body)
        for c in root.findall("%sContents" % _NS):
            key = urllib.parse.unquote(c.findtext("%sKey" % _NS, ""))
            size = int(c.findtext("%sSize" % _NS, "0") or 0)
            etag = (c.findtext("%sETag" % _NS, "") or "").strip().strip('"')
            out.append((key, size, etag))
            scanned += 1
            if copy_suffixes and os.path.splitext(key)[0].endswith(tuple(copy_suffixes)):
                n_copy += 1
        if root.findtext("%sIsTruncated" % _NS, "false").lower() != "true":
            complete = True
            break
        if max_list > 0 and scanned >= max_list:
            break
        # 本行曾漏掉 `% _NS`：findtext 的第二个参数是「默认值」，带字面 %s 的标签名永远查不到，
        # token 因此恒为空串 → 下一轮请求又回到第一页。不报错、不崩，只是数一直涨：
        # 实测把 278 个对象的目录报成「5,475,766 个对象、翻 19,697 页」（19,697×278 恰好对上），
        # 跑了 26 分钟、内存堆到 1.8GB，其实一步都没走。现在取不到 token 就直接中止喊人。
        nxt = urllib.parse.unquote(root.findtext("%sNextContinuationToken" % _NS, ""))
        if not nxt:
            sys.exit("错误：网关说这一页没列完（IsTruncated=true）却没给 NextContinuationToken，"
                     "继续请求只会反复拿同一页，已中止（本轮数据不可信，不要据此判断可删）")
        if nxt == token:
            sys.exit("错误：翻页没推进（NextContinuationToken 与上一页相同），已中止")
        token = nxt
        if stats is not None:
            stats.update(scanned=scanned, n_copy=n_copy, pages=pages, t0=t0)
        hb_scan(quiet, scanned, n_copy, pages, t0, copy_suffixes)
    if stats is not None:
        stats.update(scanned=scanned, n_copy=n_copy, pages=pages, t0=t0)
    hb_scan(quiet, scanned, n_copy, pages, t0, copy_suffixes, force=True)
    out.sort()
    return out, complete


def _conn_help(exc, endpoint):
    """连接层失败（端口拒绝 / DNS / 超时 / 代理）转成人话：刚上真机最容易撞上这个，
       而 30 行 traceback 里没任何有效信息，反而让人以为脚本坏了。"""
    reason = getattr(exc, "reason", None) or exc
    return ("错误：连不上 MinIO（%s），已中止，没下载任何文件。\n"
            "  地址：%s\n  原因：%s（%s）\n"
            "  查一查：端口是不是你那个（12001 这类，不是示例里的 9000）；服务在不在跑；\n"
            "          这台机器到它通不通（Linux：curl -sv %s；Windows：Test-NetConnection 主机 -Port 端口）；\n"
            "          要不要走代理 / NO_PROXY 里有没有漏上这个地址。"
            % (type(exc).__name__, endpoint, type(reason).__name__, reason, endpoint))


def _signed_get(endpoint, ak, sk, pathname, params, offset_sec=0, fatal_conn=False):
    now = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(seconds=offset_sec))
    query = _canonical_query(params)
    headers = build_auth(endpoint, ak, sk, "GET", pathname, query, EMPTY_SHA256, now)
    url = endpoint + pathname + ("?" + query if query else "")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        return urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError:
        raise            # 带状态码的（403/404/TimeTooSkewed）留给调用方按情况处理
    except (urllib.error.URLError, OSError) as e:
        # 列目录阶段连不上就是地址/网络错，早停；下载阶段不走这里，要留给逐项退避重试
        # （跑几十万次中间断一下很正常，不该让一个抖动毁掉整轮）
        if fatal_conn:
            sys.exit(_conn_help(e, endpoint))
        raise


def build_auth(endpoint, ak, sk, method, pathname, query, payload_hash, now):
    """SigV4 Authorization 头部（host + x-amz-date + body sha256），与处理脚本逐字节同规则。"""
    host = urllib.parse.urlparse(endpoint).netloc
    amz = _amz_date(now)
    ds = amz[:8]
    canonical_headers = ("host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n"
                         % (host, payload_hash, amz))
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, pathname, query, canonical_headers, signed_headers, payload_hash])
    scope = "%s/%s/%s/aws4_request" % (ds, REGION, SERVICE)
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz, scope,
         hashlib.sha256(canonical_request.encode()).hexdigest()])

    def h(key, s):
        return hmac.new(key, s.encode(), hashlib.sha256).digest()

    # 第一层必须把 "AWS4"+secret 先 .encode() 成 bytes（hmac 的 key 不收 str），后面几层传进
    # 去的已是 digest() 出来的 bytes。这行一度漏了 encode，后果是列目录当场 TypeError：
    # 自测把 _send 整段替换掉了、走不到签名，所以直到真连一个地址才暴露。
    signing = h(h(h(h(("AWS4%s" % sk).encode(), ds), REGION), SERVICE), "aws4_request")
    signature = hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "x-amz-date": amz,
        "x-amz-content-sha256": payload_hash,
        "Authorization": ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                          % (ak, scope, signed_headers, signature)),
    }


# ---------------- 只读下载客户端：GET 流式写盘（本文件不许出现写桶代码） ----------------

def sha256_file(path, prog=None):
    """流式算本地文件哈希：内容凭据只认这个，ETag 只当「变没变」的凭据用。"""
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            if prog is not None:
                prog.update(len(chunk))
    return sha.hexdigest()


def _retryable(code: int) -> bool:
    """值得再试的状态码：服务端内部错/繁忙/限流；4xx 里那些（404 没有、403 没权限）重试无用。"""
    return code in (408, 425, 429) or code >= 500


def _s3_err_code(body):
    """从错误响应里抓 <Code>/<Message>：HTTP 状态码常常只剩一句「400 Bad Request」，
       真正的原因（非法桶名 / 签名不对 / 对象不存在）写在 XML 里，不抓出来就没法排查。"""
    text = body.decode("utf-8", "replace")
    code = re.search(r"<Code>(.*?)</Code>", text, re.S)
    if not code:
        return ""
    msg = re.search(r"<Message>(.*?)</Message>", text, re.S)
    return "%s：%s" % (code.group(1).strip(), (msg.group(1).strip() if msg else "未给出"))


class ReadOnlyClient:
    """只有 GET。重试 + 指数退避 + 时钟偏差自动校正，其余与处理脚本一致。"""

    def __init__(self, endpoint, ak, sk, bucket, retries=4):
        self.endpoint, self.ak, self.sk = endpoint, ak, sk
        self.bucket = str(bucket).strip("/")
        self.offset = 0
        self.retries = max(1, retries)

    def _send(self, key):
        # path-style：请求 URL 和签名的 canonical URI 都必须是 /<bucket>/<key>。清单和内存
        # 里的 key 一律不含桶名（和控台上看到的一致，和处理脚本的 keys 写法有意不同），
        # 所以这里补一次：漏了的话网关会把 key 的第一段（中文目录名）当桶名，
        # 回一句 400 非法桶名，于是每个下载都失败。
        pathname = strict_encode_path("/%s/%s" % (self.bucket, str(key).lstrip("/")))
        now = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(seconds=self.offset))
        headers = build_auth(self.endpoint, self.ak, self.sk, "GET", pathname, "",
                             EMPTY_SHA256, now)
        req = urllib.request.Request(self.endpoint + pathname, headers=headers, method="GET")
        return urllib.request.urlopen(req, timeout=_TIMEOUT)

    def download(self, key, dest, expect_size=0, prog=None):
        """流式下载到 dest.part 再改名；顺带算 sha256。返回 (写入字节数, sha256, 服务端长度)。"""
        for attempt in range(self.retries):
            tmp = dest + ".part"
            try:
                resp = self._send(key)
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                off = skew_offset(body, e.headers)
                if e.code == 403 and off is not None and self.offset == 0:
                    self.offset = off
                    continue
                # 5xx/429 是服务端忙或限流，退避后重发
                if _retryable(e.code) and attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                # 其余（400 路径拼错、404 没有、403 没权限）重试也没用，但要把响应里的原因
                # 递出去，不然只剩一句「HTTP Error 400: Bad Request」，人没法查
                detail = _s3_err_code(body)
                raise RuntimeError("GET %s 失败 HTTP %s%s" % (
                    os.path.basename(key), e.code,
                    ("（服务端说：%s）" % detail) if detail else "（响应未给出原因）"))
            except (urllib.error.URLError, OSError) as e:
                if attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise RuntimeError("GET %s 网络失败：%s" % (key, e))
            total = int(resp.headers.get("Content-Length") or 0)
            sha = hashlib.sha256()
            done = 0
            try:
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if prog is not None:
                            prog.update(len(chunk))
                resp.close()
            except (OSError, urllib.error.URLError) as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise RuntimeError("写本地失败 %s：%s" % (dest, e))
            if total and done != total:
                # 连接半路断（read() 返回空但字节数不够）：留下 .part 也没用，删掉重来
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise RuntimeError("下载不完整（收到 %d / 声明 %d）" % (done, total))
            os.replace(tmp, dest)          # 原子改名：中断永远不会有半截文件
            return done, sha.hexdigest(), total
        raise RuntimeError("重试 %d 次仍失败" % self.retries)

    def sha256_of_local(self, path, prog=None):
        """跳过重下时也要有内容凭据：对本地已有文件补算一遍。"""
        return sha256_file(path, prog)


# ---------------- 清单落盘 ----------------

MANIFEST_HEADER = ("kind\tkey\tsize\tetag\tsha256\tlocal\tstatus")


def manifest_meta(prefix, suffixes, complete, n_copy, n_infl, note=""):
    """清单头部的 # 注释：让这份文件自己说得清「备的是哪个目录、哪一轮、列没列全」。
       “完整”必须记下来：否则日后 --check 会拿一份被 --max-list 截过的半清单当全集，
       算出来“全部已备齐、可以删”是完全错的结论。"""
    lines = [
        "# pdf_backup_sssss 备份清单（TSV，制表符分隔；# 开头是元信息）",
        "# prefix=%s" % prefix,
        "# suffixes=%s" % ",".join(suffixes),
        "# 列目录完整=%s" % ("yes" if complete else "no"),
        "# 生成时间=%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "# COPY行数=%d" % n_copy,
        "# INFLATED行数=%d" % n_infl,
    ]
    if note:
        lines.append("# " + note)
    return lines


def read_manifest(path):
    """读回清单，返回 ({key: 列表}, {元信息 k: v})。
       列数不够的行一划拉跳过（手工粘贴、中途损环都退化成“没这行”，不至于扫崩）。"""
    rows = {}
    meta = {}
    if not os.path.exists(path):
        return rows, meta
    try:
        with open(path, encoding="utf-8-sig") as f:
            header_seen = False
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if line.startswith("#"):
                    k, sep, v = line[1:].strip().partition("=")
                    if sep:
                        meta[k] = v
                    continue
                if not header_seen:
                    header_seen = True          # 第一行非注释就是表头
                    continue
                cols = line.split("\t")
                if len(cols) < 7:
                    continue
                rows[cols[1]] = cols
    except OSError:
        return {}, meta
    return rows, meta


def write_manifest(path, rows, meta=None):
    """整份原子重写（.tmp → replace）：TSV 和处理脚本日志同风格，awk 直接能统计。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for line in (meta or []):
            f.write(line + "\n")
        f.write(MANIFEST_HEADER + "\n")
        for r in rows:
            f.write("\t".join("" if c is None else str(c) for c in r) + "\n")
    os.replace(tmp, path)


# ---------------- 主流程 ----------------

def split_by_suffix(objects, suffixes):
    """把列到的对象分成：待备份的 _sssss 原件 / 桶里的膨胀版 / 其它。

    「膨胀版」认定 = 名字不以这些后缀结尾、且存在同名 <root>_sssss.pdf 的那个对象；
    这样只记大小+ETag，不下内容。
    """
    by_key = {k: (s, e) for k, s, e in objects}
    names = set(by_key)
    copies, inflated, others = [], [], []
    for key, size, etag in objects:
        root_ext = os.path.splitext(key)          # ('…/X', '.pdf')
        stem = root_ext[0]
        matched = next((sf for sf in suffixes if stem.endswith(sf)), None)
        if matched:
            copies.append((key, size, etag, matched))
        elif key.lower().endswith(".pdf") and any("%s%s.pdf" % (stem, sf) in names
                                                  for sf in suffixes):
            # 只把「同名且确实是 .pdf」当膨胀版记清单。成品语料目录里同一个 X 旁边还多
            # 伴了 X.md、X_alpaca.json、X_cot.json、X_qa.json 这些语料件，它们的 stem 也是 X，
            # 按原来的判法会被一并当成膨胀版：真机上这一层 190 万个对象里只有四成是 PDF，
            # 清单会从 90 万行撑到几百万行（百 MB 级）、--check 每跑一遍都要解析它们，
            # 而「膨胀版没被动过」这个结论本来也只该由 .pdf 来证。
            inflated.append((key, size, etag))
        else:
            others.append((key, size, etag))
    return copies, inflated, others


def local_path_for(out_dir, key, prefix_root, recursive):
    """默认扁平存放（同一层不可能重名，且能避开 Windows 260 字符路径上限）；
       --recursive 时按相对路径镜像子目录。"""
    name = os.path.basename(key)
    if not recursive:
        return os.path.join(out_dir, "objects", safe_name(name))
    rel = key[len(prefix_root):] if prefix_root and key.startswith(prefix_root) else name
    parts = [safe_name(p) for p in rel.split("/") if p]
    return os.path.join(out_dir, "objects", *parts)


def build_paired(copies, inflated, suffixes):
    """为每个 _sssss 找同名膨胀版，找不到就报警——配对不齐说明桶状态异常
       （别的机器正在处理，或者上一轮败在覆盖之前），这种目录不能按「备份完=可删」判断。"""
    infl_by_key = {k: (s, e) for k, s, e in inflated}
    pairs, missing_infl = [], []
    for key, size, etag, sf in copies:
        root = key[:-(len(sf) + len(".pdf"))] if key.endswith(".pdf") else key[:-(len(sf))]
        inflated_key = root + ".pdf"
        got = infl_by_key.get(inflated_key)
        if got:
            pairs.append((key, size, etag, sf, inflated_key, got[0], got[1]))
        else:
            pairs.append((key, size, etag, sf, inflated_key, None, None))
            missing_infl.append(key)
    return pairs, missing_infl


def copies_without_inflated(infl_rows, copy_keys, suffixes):
    """从清单反推「当初没配上膨胀版」的原件：这类 INFLATED 行大小为空（配不上时填的是 None）。
       这些原件同层没有 X.pdf，说明上一轮处理根本没做完；而 _sssss 是重做膨胀版的唯一凭据
       —— 删掉就再也开不出这份原件，所以它们绝对不能进可删清单。"""
    keys = set(copy_keys)
    gone = set()
    for r in infl_rows:
        if not str(r[2] or "").strip() and str(r[1]).lower().endswith(".pdf"):
            root = str(r[1])[:-len(".pdf")]
            gone |= {root + sf + ".pdf" for sf in suffixes} & keys
    return gone


def write_deletable(path, keys, suffixes, expect_total, note=""):
    """可删清单：每行一个对象名（basename）。生成它就代表“这些行逐条核过”。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 本清单每行一个可删除的对象名（basename，不带桶名/目录前缀）。\n")
        f.write("# 生成条件：这些行已在本地逐项核过（存在 + 大小 + sha256），"
                "且对应的膨胀版 size/ETag 与备份那轮逐条一致。\n")
        f.write("# 建议先数一下行数是否等于桶里 %s 的个数（本轮核过 %d 个），"
                "不等就说明期间桶被动过。\n" % ("/".join(suffixes), expect_total))
        f.write("# 膨胀版 X.pdf 的校验凭据在同目录 manifest.tsv 的 INFLATED 行里，"
                "删完再跑一次 --check 复核。\n")
        if note:
            f.write("# " + note + "\n")
        for k in sorted(keys):
            f.write(os.path.basename(k) + "\n")
    return path


def write_pending_inflated(path, keys, suffixes, note=""):
    """待补名单：这些 _sssss 原件在同层找不到对应的膨胀版 X.pdf。它们不是「可删」而是
       「还欠活」，所以要单独出一份文件交人工，不能混在删除流程里。只在终端打三行样本
       不够：跑过就丢了，而这一批恰恰是要回头补做的活。
       每行一个 basename、# 开头是注释，能直接喂给处理脚本的 --files-from（它认 X_sssss.pdf）.
       keys 为空时写一份「0 行」的：待补名单必须反映现在的欠账，不能当历史快照留下。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if not keys:
            f.write("# 当前没有待补项：本目录每个 %s 原件在同层都能找到对应的膨胀版 X.pdf。\n"
                    % "/".join(suffixes))
            f.write("# 本文件留着只为刷新时间戳：上一份「绝对不能删」的名单如果一直\n"
                    "# 躺在目录里，会和新的可删清单互相打脸，反而分不清该信哪份。\n")
            f.write("# 共 0 行。\n")
            if note:
                f.write("# " + note + "\n")
            return path
        f.write("# 这些 %s 原件在同层找不到对应的膨胀版 X.pdf：上一轮处理没做完，"
                "或者成品本来就放到子目录去了。\n" % "/".join(suffixes))
        f.write("# 本地已经备份好，但它们绝对不能删 —— 是重做膨胀版的唯一凭据。\n")
        f.write("# 要补做，把本文件直接喂给处理脚本（prefix 用同一个目录）：\n")
        f.write("#   python pdf_rename_inflate_upload.py --prefix \"<同一个目录>\" "
                "--files-from \"%s\"\n" % os.path.basename(path))
        f.write("# 补做完再跑一次 --check：补上的行会从本文件消失，同时 "
                "deletable_keys.txt 会多出一行。\n")
        f.write("# 共 %d 行。\n" % len(keys))
        if note:
            f.write("# " + note + "\n")
        for k in sorted(keys):
            f.write(os.path.basename(k) + "\n")
    return path


def refresh_pending_inflated(path, keys, suffixes, note=""):
    """待补名单的统一口子：有欠账就写名单；欠账还清了也要把旧文件刷成 0 行，不让它做过期
       情报。返回刷新前旧名单里的条数（没旧文件、或本轮有欠账时返回 0，调用方据此决定要
       不要提一嘴）。"""
    if keys:
        write_pending_inflated(path, keys, suffixes, note=note)
        return 0
    if not os.path.exists(path):
        return 0
    prev = [l for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    write_pending_inflated(path, [], suffixes,
                           note=("%s上一份名单里的 %d 行本轮已全部对上。" % (note, len(prev))
                                 if prev else note))
    return len(prev)


def do_check(endpoint, ak, sk, prefix, bucket, key_prefix, suffixes, out_dir,
             manifest_path, keys_path, pending_path, args):
    """--check：不重下，只按清单逐项复核，两个用途都靠它：
       删之前跑 = 确认「备份齐了、本地一个不坏」（回滚凭据）；
       删之后跑 = 确认「膨胀版从头到尾没被动过」（这当初就是只记 size+ETag 那份清单的意义）。
       不但不写桶，连 GET 都不发：本地只读文件算 sha256，跟目录只用 ListObjectsV2。"""
    rows_by_key, meta = read_manifest(manifest_path)
    if not rows_by_key:
        sys.exit("错误：没有可读的清单：%s\n"
                 "  --check 是核对已有备份，请先跑一次备份（不带 --check）" % manifest_path)
    copy_rows = [r for r in rows_by_key.values() if r[0] == "COPY"]
    infl_rows = [r for r in rows_by_key.values() if r[0] == "INFLATED"]
    if not copy_rows:
        sys.exit("错误：清单里没有 COPY 行（%s）" % manifest_path)
    complete = (meta.get("列目录完整") or "").lower() == "yes"

    print("连接：%s（AK=%s****，地址来自%s）" % (endpoint, ak[:4], _ENDPOINT_SRC))
    print("目录：%s（%s；来自%s）" % (prefix, "递归子目录" if args.recursive else "只列这一层",
                          _PREFIX_SRC))
    print("清单：%s（COPY %d 行，INFLATED %d 行，备份那轮生成于 %s，列目录完整=%s）"
          % (manifest_path, len(copy_rows), len(infl_rows),
             meta.get("生成时间") or "未知", "是" if complete else "否"))
    if meta.get("prefix") and meta.get("prefix") != prefix:
        print("警告：清单是备份 `%s` 时生成的，与当前目录 `%s` 不一致（--out 指错了？）"
              % (meta.get("prefix"), prefix))
    print("口径：--check 只读，不重下、不写桶\n")

    t0 = time.time()
    # ---- [1/3] 本地逐行实测：存在 + 大小 + sha256（不靠进进清单里那个旧结论）----
    # 用 --with-inflated 真下过内容的那些膨胀版（r[4] 有 sha256 = 当时算过凭据）也顺手核
    # 一遍。但它的坏结果不参与「能不能删」：删 _sssss 的凭据是原件，膨胀版本体还待在桶里
    chk_rows = copy_rows + [r for r in infl_rows if r[4]]
    print("[1/3] 核对本地 %d 个备份件（原件 %d + 膨胀版 %d，存在 + 大小 + sha256）…"
          % (len(chk_rows), len(copy_rows), len(chk_rows) - len(copy_rows)))
    hb_reset()
    lock = threading.Lock()
    missing, size_bad, hash_bad, unread, infl_bad = [], [], [], [], []
    filled = [0]
    done_n = [0]

    def chk(r):
        """返回一句问题描述（None = 通过），并把实测大小/路径/哈希回填进行里。
           问题算原件的还是膨胀版的，由调用方按 r[0] 分流，免得两种东西挤一个计数。"""
        key, size = r[1], int(r[2] or 0)
        # 先按当前机器重算一遍路径：清单里存的是备份那台机器的绝对路径，
        # Linux 备的 Windows 核（或换了 --out 目录）时它必然不存在，不能就此判「本地缺失」
        cand = [local_path_for(out_dir, key, "%s/%s" % (bucket, key_prefix), args.recursive),
                r[5]]
        path = next((p for p in cand if p and os.path.exists(p)), "")
        if not path:
            r[6] = "失败:本地缺失"
            return "缺"
        got = os.path.getsize(path)
        if got != size:
            r[6] = "失败:本地大小不符(%d≠%d)" % (got, size)
            return "大小"
        try:
            sha = sha256_file(path)
        except OSError as e:
            r[6] = "失败:本地不可读 %s" % str(e)[:60]
            return "不可读"
        if r[4] and r[4] != sha:
            r[6] = "失败:sha256不符(本地内容与备份那轮不一致!)"
            return "哈希"
        if not r[4]:                     # 备份时没算成（被中断/当时跳过了），现在补上凭据
            r[4] = sha
            with lock:
                filled[0] += 1
        r[5] = path
        r[6] = "本地已核验"
        return None

    buckets = {"缺": missing, "大小": size_bad, "哈希": hash_bad, "不可读": unread}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        fut_map = {ex.submit(chk, r): r for r in chk_rows}
        for fu in as_completed(fut_map):
            r = fut_map[fu]
            why = fu.result()
            if why:
                if r[0] == "COPY":
                    buckets[why].append(r[1])
                else:
                    infl_bad.append(r[1])
            done_n[0] += 1
            # 核对阶段不下载，没进度条可刷，恒定走整行心跳（几十个 GB 要算特别久）
            hb_line("核对本地", True, done_n[0], len(chk_rows), t0,
                    "坏 %d" % (len(missing) + len(size_bad) + len(hash_bad) + len(unread)
                               + len(infl_bad)))
    bad_local = len(missing) + len(size_bad) + len(hash_bad) + len(unread)
    print("      本地核验：%d/%d 通过，有问题 %d（缺 %d、大小不符 %d、sha256不符 %d、不可读 %d）"
          % (len(copy_rows) - bad_local, len(copy_rows), bad_local,
             len(missing), len(size_bad), len(hash_bad), len(unread)))
    if len(chk_rows) > len(copy_rows):
        print("      膨胀版本地副本：核 %d 个，有问题 %d（不参与「能不能删」，只是本地这层保险）"
              % (len(chk_rows) - len(copy_rows), len(infl_bad)))
        if infl_bad:
            print("      样本：%s" % "、".join(os.path.basename(k) for k in infl_bad[:5]))
    for tag, lst in (("sha256 不符", hash_bad), ("本地缺失", missing),
                     ("大小不符", size_bad), ("不可读", unread)):
        if lst:
            print("      %s 样本：%s" % (tag, "、".join(os.path.basename(k) for k in lst[:5])))

    # ---- [2/3] 列一次目录：拿当前 size/ETag 与清单对账 ----
    print("\n[2/3] 列目录对账（只列一次，不下载）…")
    hb_reset()
    scan = {}
    t_scan = time.time()
    try:
        objs, cur_complete = list_objects(endpoint, ak, sk, bucket, key_prefix, True,
                                         args.recursive, args.max_list,
                                         copy_suffixes=suffixes, stats=scan)
    except KeyboardInterrupt:
        interrupt_note(scan, t_scan, "--check 对账列目录", suffixes,
                       note="桶里一个对象都没动过；本地也没写任何东西（核对结果还没落盘）。")
        sys.exit(130)
    cur = {k: (s, e) for k, s, e in objs}
    infl_same = infl_changed = infl_gone = infl_norecord = 0
    changed_keys, gone_keys = [], []
    for r in infl_rows:
        got = cur.get(r[1])
        if not r[2]:
            # 备份那轮这个膨胀版本来就缺（_sssss 建了但没覆盖成功），现在有了就顺手补记
            infl_norecord += 1
            if got:
                r[2], r[3] = str(got[0]), got[1]
                # 状态一起改掉：这一行不再是「大小为空」，--check 下次就不会再把它
                # 当成「没配上膨胀版」把对应原件挡在可删清单外——现在成品有了，能删了
                r[6] = "补记(备份时缺)"
            continue
        if got is None:
            infl_gone += 1
            gone_keys.append(r[1])
            r[6] = "变动:不见了"
        elif str(got[0]) != r[2] or (got[1] and r[3] and got[1] != r[3]):
            infl_changed += 1
            changed_keys.append(r[1])
            r[6] = "变动:大小或ETag变了"
        else:
            infl_same += 1
            # 本轮实测记进行里，这份清单本身就是给验收用的凭据；[1/3] 发现本地副本
            # 有问题的要带上，不然这一句「已核(未变)」会把坏消息盖掉
            r[6] = "已核(未变)" + ("；本地副本有问题" if r[1] in set(infl_bad) else "")
    copies_now, _c_infl, _c_other = split_by_suffix(objs, suffixes)
    cur_copies = {k: s for k, s, _e, _sf in copies_now}
    mkeys = {r[1] for r in copy_rows}
    still = sorted(set(cur_copies) & mkeys)
    already_gone = sorted(mkeys - set(cur_copies))
    new_since = sorted(set(cur_copies) - mkeys)

    print("      膨胀版凭据比对：未变 %d、变了 %d、没了 %d、备份时缺失现补记 %d（共 %d 行）"
          % (infl_same, infl_changed, infl_gone, infl_norecord, len(infl_rows)))
    if changed_keys:
        print("      变了 样本：%s" % "、".join(os.path.basename(k) for k in changed_keys[:5]))
    if gone_keys:
        print("      没了 样本：%s" % "、".join(os.path.basename(k) for k in gone_keys[:5]))
    print("      桶里 %s：当前 %d 个，清单 %d 个；已删掉 %d、不在清单里（备份后新增）%d"
          % ("/".join(suffixes), len(cur_copies), len(copy_rows),
             len(already_gone), len(new_since)))
    if not cur_complete:
        print("      警告：本次目录没列完（--max-list），对账结论只能局部成立")
    if not complete:
        print("      警告：备份那轮清单本身没列完（清单头 列目录完整=no）"
              "→ 不要按目录整体删，只按可删清单逐行删")

    # ---- [3/3] 结论：把两件互相独立的事分开说清楚 ----
    print("\n[3/3] 结论")
    hard = []
    if bad_local:
        hard.append("本地备份有 %d 个不合格（上面有样本），回滚凭据不完整" % bad_local)
    if infl_changed:
        hard.append("有 %d 个膨胀版与备份那轮不一致（大小或 ETag 变了）——这正是你担心的情形" % infl_changed)
    if infl_gone:
        hard.append("有 %d 个膨胀版在桶里找不到了（被删了或改过名！）" % infl_gone)
    if new_since:
        hard.append("桶里有 %d 个 %s 不在清单内（备份之后新增的，没备过）"
                    % (len(new_since), "/".join(suffixes)))
    print("  本地备份可用性：%s" % ("✓ %d 个原件全在，大小与 sha256 逐项一致" % (len(copy_rows) - bad_local)
                                  if not bad_local else "✗ 见上面样本"))
    print("  膨胀版未被改动：%s" % ("✓ 清单里每条 size+ETag 都对得上（含已删完 _sssss 的目录）"
                                    if not infl_changed and not infl_gone else "✗ 有变动，先查清再说"))

    # 先算出「本轮实测确认可删」的那批，好让结论能一起进清单头：--check 是整份重写头部的，
    # 不算的话备份轮那行「结论=」会被盖掉，日后只翻这份文件就看不出当初判到哪一步。
    # 几十万行必须用字典查，不能拿 next(… for …) 逐行线性找（那是平方级耗时，看着像卡死）
    verified = {r[1] for r in copy_rows if r[6].startswith("本地已核验")}
    msize = {r[1]: int(r[2] or 0) for r in copy_rows}
    keys = [k for k in still if k in verified and cur_copies[k] == msize[k]]
    # 同层没配上膨胀版的原件一律排除（重做全靠它，删了就开不出原件了）
    no_infl = copies_without_inflated(infl_rows, mkeys, suffixes)
    excluded = sorted(set(keys) & no_infl)
    keys = [k for k in keys if k not in no_infl]
    check_verdict = ("先别删" if hard else
                     ("已全部删完" if not still else
                      "可删 %d 个" % len(keys)
                      + ("，另排除 %d 个同层无膨胀版" % len(excluded) if excluded else "")))
    write_manifest(manifest_path,
                   sorted(rows_by_key.values(), key=lambda r: (r[0], r[1])),
                   manifest_meta(prefix, suffixes, complete, len(copy_rows), len(infl_rows),
                                 note="本轮 --check 复核于 %s；本地不合格 %d、膨胀版变动 %d；结论=%s"
                                      % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                         bad_local, infl_changed + infl_gone, check_verdict)))
    print("  清单已回填实测值：%s（补算 sha256 %d 条）" % (manifest_path, filled[0]))

    if hard:
        print("\n结论：先别删。")
        for s in hard:
            print("  · " + s)
        print("  重跑本脚本（不带 --check）会把缺的接着备，再 --check 直到两个✓。")
        return
    if not still:
        # 清单里的 _sssss 已经全部不在桶里了 = 这一批删完了
        print("\n结论：这一批已全部删完，且删的过程中膨胀版一个字节都没变（清单凭据逐条对得上）。")
        print("      本地备份（%d 个原件 + %d 条膨胀版凭据）请至少留到成品语料验收完。" %
              (len(copy_rows), len(infl_rows)))
        return
    # 可删清单和排除项已在上面算结论时一起筛好了（同一套凭据，不重复筛第二遍）
    if not keys:
        print("\n结论：没有可删项——清单里的原件不是本地没核过，就是同层还没有膨胀版（见上）。")
        return
    write_deletable(keys_path, keys, suffixes, len(keys),
                    note="本清单由 --check 生成于 %s；只包含逐条核过的行，"
                         "请按清单逐行删，不要按目录整体删。" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("\n结论：可以删（按清单逐行删）。可删清单：%s（%d 行，已按实测刷新）"
          % (keys_path, len(keys)))
    # 同层还欠膨胀版的原件单列一份待补名单：拿 no_infl 全集而不是只看本轮剔掉的
    # excluded——只要清单里还挂着空凭据行、且现在桶里仍没这个 X.pdf，就都还是欠活
    pending_all = sorted(no_infl)
    if pending_all:
        refresh_pending_inflated(pending_path, pending_all, suffixes,
                                 note="本清单由 --check 生成于 %s。"
                                      % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("      待补膨胀版：%d 个原件同层还没有 X.pdf（备份那轮就没配上），已单列清单："
              "%s（%d 行）" % (len(pending_all), pending_path, len(pending_all)))
        print("        样本：%s" % "、".join(os.path.basename(k) for k in pending_all[:3]))
        print("        它们不参与「能不能删」，也不能删：补完再 --check，"
              "这些行会从这里消失、并进可删清单。")
    else:
        # 同备份轮：--check 发现欠账已还清，就把旧名单刷成 0 行，免得它一直挂着让人以为还有活要补
        cleared = refresh_pending_inflated(pending_path, [], suffixes,
                                          note="本清单由 --check 刷新于 %s。"
                                               % time.strftime("%Y-%m-%d %H:%M:%S"))
        if cleared:
            print("      待补膨胀版：本轮已全部对上（上一份名单里的 %d 行已补完），"
                  "名单已刷新为 0 行：%s" % (cleared, pending_path))
    print("      删完后再跑一次 --check，看到“已全部删完且膨胀版未变”才算收尾。")
    print("      用时 %s" % human_secs(time.time() - t0))


def disk_free(path):
    """问目标盘剩多少。不能直接拿 out_dir 问：磁盘还没建（干跑不建目录、或者第一次备份
       前）时 shutil.disk_usage 会报 FileNotFoundError。沿路往上找到第一个真存在的祖先目录
       再问——同一块盘上问哪个目录得到的都是整盘余量，结果不变。"""
    p = os.path.abspath(path)
    while True:
        try:
            return shutil.disk_usage(p).free
        except FileNotFoundError:
            parent = os.path.dirname(p)
            if not parent or parent == p:
                # 一路退到根（或盘符本身）还不存在 = 进错盘了，不是“子目录没建”那么简单
                sys.exit("错误：读不到盘符——从 %s 一路往上到 %s 都不存在。\n"
                         "  --out 指错了？盘符没挂载/网络盘断了？"
                         % (os.path.abspath(path), parent))
            p = parent
        except OSError as e:
            sys.exit("错误：读不到 %s 所在盘的剩余空间：%s" % (p, e))


def estimate_local(pairs, out_dir, bucket, key_prefix, recursive):
    """下载前先扫一遍本地：已存在且大小一致的会跳过重下（也不新占盘），所以
       「还要新写多少」比「总共多少」更能反映本轮真吃掉多少空间——拿总量去对比自
       由空间，断点续跑那一轮会虚高一倍、把你误拦在门外。几十万次 stat 走线程池
       （纯元数据操作，一般几秒完事）。返回 (可复用字节, 可复用个数, 待新下个数)。"""
    def one(p):
        dest = local_path_for(out_dir, p[0], "%s/%s" % (bucket, key_prefix), recursive)
        try:
            if os.path.getsize(dest) == p[1]:
                return p[1]
        except OSError:
            pass            # 不存在/不可读都当“没备过”，归到待新下
        return 0

    reuse = reuse_cnt = miss_cnt = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for v in ex.map(one, pairs):
            if v:
                reuse += v
                reuse_cnt += 1
            else:
                miss_cnt += 1
    return reuse, reuse_cnt, miss_cnt


def confirm_start(args, enough, new_need, usable, out_dir):
    """下载前那道闸：盘不够一定拦（这种情形连 --yes 也不放行），盘够也要有人点头才动手。"""
    if args.dry_run:
        return                                  # 干跑不写盘，不用问
    if not enough:
        sys.exit("错误：目标盘装不下（还要新写 %s，按余量算可用只有 %s）。已中止，没下载任何文件。\n"
                 "  办一：把 --out 换到更大的盘（当前：%s）\n"
                 "  办二：加 --limit 分批备，本轮先备一部分\n"
                 "  办三：先腾盘；余量要求确实过大可调 --min-free（默认 1024MiB，不建议调到 0）"
                 % (human_size(new_need), human_size(usable), os.path.abspath(out_dir)))
    if args.yes:
        print("（--yes：跳过人工确认，直接开始下载）\n")
        return
    if not (sys.stdin and sys.stdin.isatty()):
        sys.exit("错误：现在不是交互终端（nohup / 输出被重定向 / 在管道里跑），问不到人。\n"
                 "  账目确认没问题就加 --yes 再跑一次（下载已开始就会接着上面的备）；\n"
                 "  只想看账就把 --yes 换成 --dry-run。")
    try:
        ans = input("确认开始备份？[y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans in ("y", "yes"):
        return
    print("已取消：没有下载任何文件（本地已有的备份件保持原样）。")
    sys.exit(0)


def interrupt_note(scan, t0, where, suffixes, note=None):
    """Ctrl+C 停在列目录时，只丢一句「已中断」太薄。人此刻需要三件事：扫到哪了、
       有没有留下烂摊子、下次怎么少受罪。一次说清。"""
    el = max(0.001, time.time() - t0)
    print("\n已中断（%s阶段，用时 %s）。" % (where, human_secs(el)))
    if scan.get("scanned"):
        print("  列目录扫到 %s 个对象就停了（其中要备的 %s %s 个，翻了 %s 页，"
              "平均约 %s 个/秒）—— 这个目录确实大。"
              % (format(scan["scanned"], ","), "/".join(suffixes),
                 format(scan["n_copy"], ","), format(scan["pages"], ","),
                 format(int(scan["scanned"] / el), ",")))
    print("  " + (note or "桶里一个对象都没动过（本脚本全程只读），本地清单也还没开始写；"
                        "最多只剩一个空的 objects 目录，想清掉随时能删。"))
    print("  硬列完整不值当，三条路：")
    print("    ① 先摸底：--max-list 20000，列够就停（清单会标「列目录完整=no」，据此禁止删除）")
    print("    ② 分批跑：--prefix 指到更深一层的子目录，一个子目录一轮")
    print("    ③ 放后台：把输出重定向到文件，非终端时每 %.0f 秒报一行整行进度"
          "（前台是原地刷新）" % _HEARTBEAT)
    print("    心跳想更勤：环境变量 PDF_HEARTBEAT=2（默认 5 秒）")


def main():
    ap = argparse.ArgumentParser(
        description="把一个目录的 _sssss 原件备份到本地 + 给膨胀版出校验清单"
                    "（只读，绝不写桶；--prefix 目录必填）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--endpoint", help="MinIO 地址，例 http://10.0.0.1:9000（也可用环境变量）")
    ap.add_argument("--access-key", help="MinIO Access Key（默认取本文件 CONFIG）")
    ap.add_argument("--secret-key", help="MinIO Secret Key")
    # 不用 argparse 的 required=True：它会在 resolve_prefix 之前就按英文报错退出，把
    # 「环境变量 PDF_PREFIX 给目录」这条后台包装要用的路一并堵掉（--endpoint 也是自己校的）。
    ap.add_argument("--prefix",
                    help="【必填】要备份的目录 <bucket>/<路径>，开头不带 /；"
                          "脚本不留默认值也不读 .env，只认 --prefix 或环境变量 PDF_PREFIX")
    ap.add_argument("--out", help="本地备份目录（默认 脚本目录/backup/<目录尾段>）")
    ap.add_argument("--suffix", default=CONFIG["SUFFIX"],
                    help="要备份的对象后缀，逗号分隔；只比对名字，不含 .pdf")
    ap.add_argument("-j", "--jobs", type=int, default=CONFIG["JOBS"], help="并发下载线程数")
    ap.add_argument("-n", "--limit", type=int, default=0, help="最多备份几个（0=不限）")
    ap.add_argument("--max-list", type=int, default=0,
                    help="最多列多少个对象就停（大目录先拿它摸底；停过就代表清单不完整，禁止删）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单与容量估算，不下载")
    ap.add_argument("--min-free", type=int, default=1024, metavar="MIB",
                    help="目标盘至少留下的余量（MiB）；算下来不够就不开始备份")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="跳过下载前的人工确认（nohup/重定向等问不到人的场合必须带）")
    ap.add_argument("--check", action="store_true",
                    help="不重下，只按清单核对本地文件（大小+sha256）并重新对账桶")
    ap.add_argument("--force", action="store_true", help="本地已存在也重新下载")
    ap.add_argument("--with-inflated", action="store_true",
                    help="把每个 X_ssssss.pdf 对应的膨胀版 X.pdf 也下载下来（默认只记大小+ETag）"
                         "；体积通常翻十几倍，会计入下载前的容量预检")
    ap.add_argument("--recursive", action="store_true",
                    help="连子目录一起备（默认只列一层，和处理脚本口径一致）")
    ap.add_argument("--no-relist", action="store_true",
                    help="结束后不再列一次目录对账（默认要列，这是能不能删的关键一步）")
    ap.add_argument("-q", "--quiet", action="store_true", help="静默：不刷单文件进度条")
    args = ap.parse_args()

    ensure_utf8_streams()
    endpoint, ak, sk = resolve_conn(args)
    prefix, bucket, key_prefix = resolve_prefix(args)
    suffixes = tuple(s.strip() for s in (args.suffix or "").split(",") if s.strip())
    if not suffixes:
        sys.exit("错误：--suffix 不能为空")
    quiet = bool(args.quiet) or not sys.stdout.isatty()
    cols = shutil.get_terminal_size((100, 24)).columns

    tail = [p for p in key_prefix.strip("/").split("/") if p][-2:] or ["root"]
    out_dir = args.out or os.path.join(_SCRIPT_DIR, "backup", "_".join(tail))
    obj_dir = os.path.join(out_dir, "objects")
    manifest_path = os.path.join(out_dir, "manifest.tsv")
    keys_path = os.path.join(out_dir, "deletable_keys.txt")
    # 同层还欠膨胀版的原件单独一份名单：它们不参与删除，只提醒人回头补做
    pending_path = os.path.join(out_dir, "pending_inflated_keys.txt")
    if args.check:
        # 核对模式走自己那套完整流程（本地实测 + 列目录对账 + 出结论），不进备份主线；
        # 放在 makedirs 前面是因为它只读不写目录，没备份过时报错也不要留个空目录
        do_check(endpoint, ak, sk, prefix, bucket, key_prefix, suffixes, out_dir,
                 manifest_path, keys_path, pending_path, args)
        return
    if not args.dry_run:
        # 干跑连目录都不建：它承诺「只列清单不下载」，留个空文件夹出来反而让人分不清
        # 到底备过没备过（后面接着真跑时再由下载分支建）
        os.makedirs(obj_dir, exist_ok=True)

    print("连接：%s（AK=%s****，region=%s，地址来自%s）"
          % (endpoint, ak[:4], REGION, _ENDPOINT_SRC))
    print("目录：%s（%s；来自%s）" % (prefix, "递归子目录" if args.recursive else "只列这一层",
                          _PREFIX_SRC))
    print("备份后缀：%s；本地目录：%s" % ("、".join(suffixes), out_dir))
    print("口径：只读脚本，全程只有 GET 与 ListObjectsV2，不会改动桶\n")

    t_all = time.time()
    scan = {}
    print("正在列目录…（对象多时要翻很多页，这行下面每 %.0f 秒刷一次进度；"
          "想先摸底就重跑加 --max-list 20000）" % _HEARTBEAT)
    try:
        objs, complete = list_objects(endpoint, ak, sk, bucket, key_prefix, quiet,
                                      args.recursive, args.max_list, copy_suffixes=suffixes,
                                      stats=scan)
    except KeyboardInterrupt:
        interrupt_note(scan, t_all, "首次列目录", suffixes)
        sys.exit(130)
    copies, inflated, others = split_by_suffix(objs, suffixes)
    pairs, missing_infl = build_paired(copies, inflated, suffixes)
    total_bytes = sum(p[1] for p in pairs)
    if args.limit and args.limit > 0:
        pairs_use = pairs[:args.limit]
    else:
        pairs_use = pairs
    print("列目录完成：%d 个对象，用时 %s；其中待备份 %s %d 个（%s），"
          "对应膨胀版 %d 个（只记大小+ETag，不下内容）"
          % (len(objs), human_secs(time.time() - t_all), "/".join(suffixes), len(pairs),
             human_size(total_bytes), len(inflated)))
    if missing_infl:
        print("警告：%d 个 _sssss 在同层找不到对应膨胀版（多半是上一轮败在覆盖 X.pdf 之前，"
              "或子目录里有遗留）。这批照样备份，但「备份完=可删」的判断要打个折扣"
              % len(missing_infl))
    if not complete:
        print("警告：目录没列完（--max-list 提前停了）→ 清单不完整，本轮结束后禁止删除任何对象")
    if others:
        print("（另有 %d 个对象不属于上述两类，忽略）" % len(others))

    # ---- 下载前先把账算清楚：要备多少 / 本地已有多少 / 还要新写多少 / 盘还剩多少 ----
    # 任务表把原件和（可选的）膨胀版拼成一份，行格式 [key, size, etag, kind]：
    # 前两位正好是 estimate_local 要的 (key, size)，所以预扫本地、下载、写清单三步共用
    # 这一张表，不会出现「账上算了膨胀版、下载却没下」这种两本账。
    infl_tasks = [[p[4], p[5], p[6], "INFLATED"] for p in pairs_use if p[5] is not None]
    copy_tasks = [[p[0], p[1], p[2], "COPY"] for p in pairs_use]
    tasks = copy_tasks + (infl_tasks if args.with_inflated else [])
    t_scan = time.time()
    need = sum(p[1] for p in pairs_use)
    reuse, reuse_cnt, miss_cnt = estimate_local(copy_tasks, out_dir, bucket, key_prefix,
                                                args.recursive)
    new_need = need - reuse
    need_i = sum(t[1] for t in infl_tasks)
    reuse_i = reuse_ic = miss_ic = 0
    if args.with_inflated:
        reuse_i, reuse_ic, miss_ic = estimate_local(infl_tasks, out_dir, bucket, key_prefix,
                                                    args.recursive)
        new_need += need_i - reuse_i     # 膨胀版也算进同一个闸门，不够就一起拦下
    min_free = max(0, int(args.min_free)) * _MIB
    free = disk_free(out_dir)
    usable = free - min_free
    enough = new_need <= usable
    print("\n盘算（还没下载，本地只做了 stat 扫一眼，用时 %s）："
          % human_secs(time.time() - t_scan))
    print("  本轮要备份 %d 个 %s，合计 %s"
          % (len(pairs_use), "/".join(suffixes), human_size(need)))
    if args.with_inflated:
        print("  膨胀版     %d 个（--with-inflated：一并下载），合计 %s；本地已有 %d 个（%s），"
              "还要新写 %d 个 %s"
              % (len(infl_tasks), human_size(need_i), reuse_ic, human_size(reuse_i),
                 miss_ic, human_size(need_i - reuse_i)))
    else:
        print("  膨胀版     %d 个、合计 %s 不下载（只往清单里记大小+ETag；要一起备就加 "
              "--with-inflated）" % (len(infl_tasks), human_size(need_i)))
    if args.with_inflated:
        print("  本地已有   %d 个（%s）可跳过不重下；还要新写 %d 个（原件 %d + 膨胀版 %d），%s"
              % (reuse_cnt + reuse_ic, human_size(reuse + reuse_i), miss_cnt + miss_ic,
                 miss_cnt, miss_ic, human_size(new_need)))
    else:
        print("  本地已有   %d 个（%s）可跳过不重下；还要新写 %d 个，%s"
              % (reuse_cnt, human_size(reuse), miss_cnt, human_size(new_need)))
    if args.force and (reuse_cnt or reuse_ic):
        print("  （--force：%d 个已存在的会重下覆盖，盘上不会双份，但要多跑 %s 传输）"
              % (reuse_cnt + reuse_ic, human_size(reuse + reuse_i)))
    print("  目标盘     %s 剩余 %s；按 --min-free 预留 %s → 可用 %s"
          % (os.path.abspath(out_dir), human_size(free),
             human_size(min_free), human_size(usable)))
    print("  结论         %s" % ("够，下完预计还能用 %s" % human_size(usable - new_need)
                                if enough else "不够，还差 %s" % human_size(new_need - usable)))
    if not enough and args.with_inflated and (need_i - reuse_i) > (need - reuse):
        # 账不够时先说哪一笔是大头，不然人只会去查原件那边
        print("  （大头是膨胀版：%s vs 原件 %s；想去掉膨胀版就重跑不带 --with-inflated，"
              "或先用 --limit 分批）" % (human_size(need_i - reuse_i), human_size(need - reuse)))
    confirm_start(args, enough, new_need, usable, out_dir)

    if args.dry_run:
        print("\n干跑：只列清单不下载。前 10 条预览：")
        for p in pairs_use[:10]:
            print("  %s（%s） ←→ 膨胀版 %s（%s，etag=%s）"
                  % (os.path.basename(p[0]), human_size(p[1]), os.path.basename(p[4]),
                     human_size(p[5]) if p[5] else "缺失", (p[6] or "-")[:12]))
        print("去掉 --dry-run 即开始下载%s（会先把上面这份账再打一遍并要你确认）。"
              % ("，含膨胀版" if args.with_inflated else ""))
        return

    # 断点续跑靠本地文件存在且大小一致来判（不重下），本轮结束后整份重写清单
    client = ReadOnlyClient(endpoint, ak, sk, bucket, int(CONFIG["RETRIES"]))
    rows, done_n, skip_n, fail_n = [], 0, 0, 0
    t0 = time.time()
    total_n = len(tasks)

    def fetch(t):
        nonlocal done_n, skip_n, fail_n
        key, size, etag, kind = t
        dest = local_path_for(out_dir, key, "%s/%s" % (bucket, key_prefix), args.recursive)
        # 标一下这条下的是原件还是膨胀版：两份文件同放一个 objects 目录，名字又只差后缀，
        # 不打标在进度条上看不出下的是哪一份（原件几十 KB，膨胀版八九 MB）
        tag = "膨胀版 " if kind == "INFLATED" else ""
        name = os.path.basename(key)
        status, sha = "", ""
        if os.path.exists(dest) and os.path.getsize(dest) == size and not args.force:
            try:
                sha = client.sha256_of_local(dest)
                status = "已存在跳过"
            except OSError as e:
                status = "失败:本地不可读 %s" % e
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            prog = Progress("下载 %s%s" % (tag, name), size, quiet, cols, _lock)
            try:
                got, sha, _ = client.download(key, dest, size, prog)
                if got != size:
                    status = "失败:大小不符 %d≠%d" % (got, size)
                else:
                    status = "已下载"
            except Exception as e:
                status = "失败:%s" % str(e)[:80]
            finally:
                prog.close()
        with _tally_lock:
            if status == "已下载":
                done_n += 1
            elif status == "已存在跳过":
                skip_n += 1
            else:
                fail_n += 1
            n = done_n + skip_n + fail_n
        if not quiet:
            print("[%d/%d] %s %s：%s %s sha256=%s"
                  % (n, total_n, "✓" if not status.startswith("失败") else "✗",
                     tag + name, status, human_size(size), (sha or "-")[:12]))
        elif n % 50 == 0 or n == total_n:
            with _lock:
                sys.stderr.write("[%s] 已备份 %d/%d（新下 %d、跳过 %d、失败 %d）\n"
                                 % (human_secs(time.time() - t0), n, total_n,
                                    done_n, skip_n, fail_n))
                sys.stderr.flush()
        return [kind, key, size, etag, sha, dest if os.path.exists(dest) else "", status]

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [ex.submit(fetch, t) for t in tasks]
        try:
            for fu in as_completed(futs):
                rows.append(fu.result())
        except KeyboardInterrupt:
            for fu in futs:
                fu.cancel()
            print("\n中断：本轮没跑完，清单已落盘但不完整 —— 先别删。")
            write_manifest(manifest_path, rows,
                           manifest_meta(prefix, suffixes, False,
                                         len([r for r in rows if r[0] == "COPY"]),
                                         len([r for r in rows if r[0] == "INFLATED"]),
                                         note="本轮被 Ctrl+C 中断，清单不完整"))
            raise

    # 膨胀版的清单行：没下就只记大小+ETag（靠 --check 事后对账）；真下了就把实测的
    # sha256 和本地路径一并填进去，这份清单同时就是膨胀版本体的凭据
    if args.with_inflated:
        infl_rows = [r for r in rows if r[0] == "INFLATED"]
        # 本轮真下过的只有几条：剩下那些（被 --limit 截掉的、或桶里本来就没膨胀版的）
        # 仍得按全量补上凭据行，不然清单会从「全量凭据」缩水成「本轮下过的那几条」，
        # --check 再跑就没东西可对；大小为空的那些行也正是 --check 用来认「缺配对」的记号
        got_i = {r[1] for r in infl_rows}
        infl_rows += [["INFLATED", ik, isize if isize is not None else "", ietag or "", "", "",
                       "清单(不下内容)"]
                      for (_k, _s, _e, _sf, ik, isize, ietag) in pairs if ik not in got_i]
    else:
        infl_rows = [["INFLATED", ik, isize if isize is not None else "", ietag or "", "", "",
                      "清单(不下内容)"] for (_k, _s, _e, _sf, ik, isize, ietag) in pairs]
    # 只拿 rows 里的原件部：膨胀版行已经全在 infl_rows 里了（下面那两条就是同一个对象的
    # 引用），两块直接拼会把真下载那几条写两遍——清单上同一 key 出两条 INFLATED，
    # --check 读成字典时谁覆盖谁全凭排序，刚填进去的 sha256 会被空行盖掉
    all_rows = sorted([r for r in rows if r[0] == "COPY"] + infl_rows,
                      key=lambda r: (r[0], r[1]))
    meta_lines = manifest_meta(prefix, suffixes, complete,
                               len([r for r in rows if r[0] == "COPY"]), len(infl_rows))
    write_manifest(manifest_path, all_rows, meta_lines)

    # 「能不能删」只看原件：膨胀版还好端端待在桶里，它下没下成功不影响回滚凭据是否齐
    ok_rows = [r for r in rows if r[0] == "COPY" and r[6] in ("已下载", "已存在跳过")]
    fail_rows = [r for r in rows if r[0] == "COPY" and r[6].startswith("失败")]
    ok_infl = [r for r in rows if r[0] == "INFLATED" and r[6] in ("已下载", "已存在跳过")]
    fail_infl = [r for r in rows if r[0] == "INFLATED" and r[6].startswith("失败")]

    # ---- 对账：再列一次目录，确认 S == S'，并且本地一个都不缺 ----
    verdict = "先别删"
    if not args.no_relist:
        print("\n重新列目录对账（确认备份期间桶没被动过）…")
        scan2, t_re = {}, time.time()
        try:
            objs2, complete2 = list_objects(endpoint, ak, sk, bucket, key_prefix, quiet,
                                           args.recursive, args.max_list,
                                           copy_suffixes=suffixes, stats=scan2)
        except KeyboardInterrupt:
            interrupt_note(scan2, t_re, "对账列目录", suffixes,
                           note="桶里一个对象都没动过；已下载的备份件全部在本地，重跑会跳过；"
                                "但本轮清单还没落盘，别按半截清单删东西。")
            sys.exit(130)
        copies2, _i2, _o2 = split_by_suffix(objs2, suffixes)
        # 基线用「第一轮列到的全集」而不是「本轮下成功的」：后者会把下载失败、
        # 或 -n 没做到底的项误报成“桶里新增了一个、有机器在跑！”，把人的注意力带偏
        set1 = {k: (s, e) for k, s, e, _sf in copies}
        set2 = {k: (s, e) for k, s, e, _sf in copies2}
        new_since = sorted(set(set2) - set(set1))
        gone_since = sorted(set(set1) - set(set2))
        changed = sorted(k for k in set(set1) & set(set2) if set1[k] != set2[k])
        # 膨胀版内容没下载，但它本来就不该在这期间被动过：拿 size/ETag 再核一次。
        # 对不上就是有机器正在改写这个目录，这时候删 _sssss 等于在别人的作业上动土
        cur2 = {k: (s, e) for k, s, e in objs2}
        infl_gone = [r for r in infl_rows if r[2] and r[1] not in cur2]
        infl_changed = [r for r in infl_rows if r[2] and r[1] in cur2 and (
            str(cur2[r[1]][0]) != str(r[2])
            or (cur2[r[1]][1] and r[3] and cur2[r[1]][1] != r[3]))]
        if new_since:
            print("对账：备份期间桶里新增 %d 个 _sssss（有机器在跑这个目录！）" % len(new_since))
        if gone_since:
            print("对账：备份期间桶里少了 %d 个（被别人删了/动了）" % len(gone_since))
        if changed:
            print("对账：%d 个 _sssss 大小或 ETag 变了" % len(changed))
        if infl_changed or infl_gone:
            print("对账：膨胀版 %d 个内容变了、%d 个不见了（备份期间这个目录被改写过，"
                  "先查清是谁在跑）" % (len(infl_changed), len(infl_gone)))
        if (not new_since and not gone_since and not changed and not fail_rows
                and not infl_changed and not infl_gone
                and complete and complete2 and len(ok_rows) == len(pairs)):
            verdict = "可以删"
    else:
        print("（--no-relist：跳过对账，无法证明备份期间桶没被动过）")

    print("\n===== 备份结束 =====")
    print("对象：%d 个待备份；成功 %d（新下 %d、已存在跳过 %d），失败 %d"
          % (len(pairs), len(ok_rows),
             len([r for r in ok_rows if r[6] == "已下载"]),
             len([r for r in ok_rows if r[6] == "已存在跳过"]), len(fail_rows)))
    if args.with_inflated:
        print("膨胀版：%d 个已到本地（新下 %d、跳过 %d），失败 %d"
              % (len(ok_infl), len([r for r in ok_infl if r[6] == "已下载"]),
                 len([r for r in ok_infl if r[6] == "已存在跳过"]), len(fail_infl)))
        if fail_infl:
            # 膨胀版下失败不影响「能不能删」（桶里那份还在），但本地多出来的保险缺了几块
            print("  失败样本：%s" % "、".join(
                "%s→%s" % (os.path.basename(r[1]), r[6][:40]) for r in fail_infl[:3]))
            print("  重跑本命令即可接着下（已下好的会跳过）")
    print("清单：%s（含 %d 条 COPY + %d 条膨胀版 INFLATED 校验记录）"
          % (manifest_path, len([r for r in rows if r[0] == "COPY"]), len(infl_rows)))
    if args.with_inflated:
        print("容量：本地 %s，其中膨胀版 %s（和原件放同一层 objects 目录，靠文件名区分）"
              % (human_size(sum(int(r[2] or 0) for r in rows)),
                 human_size(sum(int(r[2] or 0) for r in rows if r[0] == "INFLATED"))))
    else:
        print("容量：本地 %s；膨胀版未下载（合计 %s，只留了大小+ETag）"
              % (human_size(sum(int(r[2] or 0) for r in rows)),
                 human_size(sum(int(r[2] or 0) for r in infl_rows))))
    print("用时：%s" % human_secs(time.time() - t0))

    # 找不到膨胀版的原件不进可删清单：同层没有 X.pdf = 上一轮处理没做完，而这个 _sssss
    # 是重做膨胀版的唯一凭据，删掉就再也开不出这份原件了
    keep_out = set(missing_infl)
    if verdict == "可以删":
        dl_keys = [r[1] for r in ok_rows if r[1] not in keep_out]
        if dl_keys:
            write_deletable(keys_path, dl_keys, suffixes, len(dl_keys),
                            note="本清单由备份轮生成于 %s%s。" % (
                                time.strftime("%Y-%m-%d %H:%M:%S"),
                                ("；已排除 %d 个同层还没有膨胀版的原件" % len(keep_out))
                                if keep_out else ""))
            print("结论：可以删。可删清单：%s（%d 行）" % (keys_path, len(dl_keys)))
        else:
            print("结论：本轮没有可删项（唯一那些都被下面的待补名单排除了）。")
    else:
        if fail_rows:
            print("失败样本（前 5）：")
            for r in fail_rows[:5]:
                print("  %s → %s" % (os.path.basename(r[1]), r[6]))
        print("结论：先别删。要么有机器还在跑这个目录，要么上面有失败项；"
              "重跑本脚本（会自动接着备）直到出现「可以删」。")

    # 不管本轮结论是什么，「同层还欠一个膨胀版」的原件都要留一份名单给人工：
    # 光在终端打三行样本，跑过就丢了，而这一批恰恰是要回头补做的活
    if keep_out:
        # 能走到这里就肯定了不是干跑（干跑在上面就 return 了），所以不用再看 args.dry_run
        refresh_pending_inflated(pending_path, keep_out, suffixes,
                                 note="本清单由备份轮生成于 %s。"
                                      % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("      待补膨胀版：%d 个原件同层还没有 X.pdf（上一轮没做完），%s："
              "%s（%d 行）"
              % (len(keep_out),
                 "已从可删清单里排除" if verdict == "可以删" else "不能删，已另出清单",
                 pending_path, len(keep_out)))
        print("        样本：%s" % "、".join(os.path.basename(k) for k in sorted(keep_out)[:3]))
        # 整条命令放在一行里打印，不然人复制的时候断成两行就不能直接跑
        print("        要补做：python pdf_rename_inflate_upload.py --prefix \"<同一目录>\" "
              "--files-from \"%s\"" % os.path.basename(pending_path))
        print("        补完再 --check：这些行会从这里消失、并进可删清单；在此之前它们绝不能删。")
    else:
        # 上一个目录有欠账、这一轮补完了：旧名单不能就在那儿躺着不提
        cleared = refresh_pending_inflated(pending_path, [], suffixes,
                                          note="本清单由备份轮刷新于 %s。"
                                               % time.strftime("%Y-%m-%d %H:%M:%S"))
        if cleared:
            print("      待补膨胀版：本轮已全部对上（上一份名单里的 %d 行已补完），"
                  "名单已刷新为 0 行：%s" % (cleared, pending_path))
    # 把最终结论也写进清单头：日后只翻这一份文件就知道当初判到哪一步了
    write_manifest(manifest_path, all_rows,
                   meta_lines + ["# 结论=%s（备份轮列目录：完整=%s，对账=%s）"
                                 % (verdict + ("，已排除 %d 个同层无膨胀版" % len(keep_out)
                                                if verdict == "可以删" and keep_out else ""),
                                    "yes" if complete else "no",
                                    "已做" if not args.no_relist else "跳过")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n已中断。本轮没跑完：列目录或下载被打断时清单不会落盘，"
                 "重跑本脚本会自动接着备；没拿到完整清单前不要删任何对象。")
