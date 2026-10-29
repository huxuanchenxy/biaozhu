#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_check_damaged.py —— 本地 PDF 批量损坏体检（只读、零第三方依赖、Windows/Linux 通用）

为什么需要它：桶里 4 万多份 PDF 中混着一些打不开的文件。本脚本用「结构层解析 + 可选外部
权威引擎交叉印证」两层判定，把“真损坏”“只是加密”“是我们旧填充规则产出的可疑文件”分开列清。
全程只读文件、不写不删（除非你显式指定 --csv/--list-file/--copy-broken 输出）。

用法（Windows 用 python，Linux 用 python3 或 ./本文件）：
  python pdf_check_damaged.py D:\\待测损坏文件          # 递归检测目录下所有 PDF
  python pdf_check_damaged.py pdf/ -n 200 -w 8 -q      # 只测前 200 个，8 线程，不刷进度条
  python pdf_check_damaged.py pdf/ --sample 500        # 4 万份先随机抽 500 摸底
  python pdf_check_damaged.py pdf/ -w 8 --csv bad.csv --list-file bad.txt   # 全量，出报告+清单
  python pdf_check_damaged.py pdf/ --broken-only                # 明细只列损坏，连可疑都不列
  ./pdf_check_damaged.py /data/pdf --no-cli            # 只用内置结构校验
  ./pdf_check_damaged.py /data/pdf --copy-broken /data/bad   # 把判定损坏的拷出来（不删源）

默认明细列出 warn+broken（正常文件不刷屏）；--show-all 才把正常文件也逐个打印。

外部权威校验（可选，装了就用、没装就跳过，不影响脚本运行）：
  qpdf --check / pdfinfo / mutool info   Linux: apt install qpdf poppler-utils mupdf-tools
  内置结论与 CLI 结论冲突时：CLI 说坏→按坏；内置说坏而 CLI 说 OK→降级为可疑（避免误报）。

结论码（明细默认只列 warn/broken 两级，正常文件不列）：
  OK                 结构完整，能定位目录树与页面
  OK_PADDED_NEW      本管线膨胀产物（新规则：填充后补了 startxref，尾部规范）
  PAD_LEGACY_TAIL    本管线膨胀产物（旧规则：几 MB 填充把 startxref 推出阅读器的尾部回搜
                     窗口，MuPDF/严格阅读器会报 “cannot find startxref” 并走修复分支，
                     部分文件直接打不开）→ 用 pdf_rename_inflate_upload.py --refix 重做
  ENCRYPTED          有 /Encrypt，属加密而非损坏
  NOT_PDF            文件头找不到 %PDF-（多半是下载失败的错误页/压缩包改名而来）
  NUL_FILE           整份几乎全是 0x00 字节（写入失败/占位空文件，从来没真正写过 PDF 内容）
  EMPTY              0 字节或小到不可能是一个 PDF
  NO_TRAILER_EOF     尾部找不到 %%EOF（下载被截断）
  NO_STARTXREF       找不到 startxref，或偏移明显越界
  XREF_MISMATCH      xref 声称的偏移处没有对应的 “N G obj”（对象错位/内容被截断）
  XREF_DECODE_FAIL   交叉引用流（/Type/XRef）解压或按 /W 解码失败
  PAGES_ZERO         解析到 Catalog 但页数解析为 0
  NO_CATALOG         trailer 的 /Root 指向的对象找不到或不是 Catalog
  STRUCT_WARN        其他可疑但不致命的结构问题（/Size 偏小、缺 /Root、偏移贴边等）
  READ_ERROR         本地读取异常（权限/占用/坏道）

退出码：0=没有 broken；1=检出 broken；2=参数或环境错误。便于接在流水线里判断。
"""

import argparse
import csv
import io
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

# ==================== 配置区（命令行可覆盖；不需要任何配置文件） ====================
CONFIG = {
    "DIR": "",              # 留空=必须用命令行传目录；给了就默认扫这个目录
    "WORKERS": 8,           # 并发线程数（本地读+解析，纯 I/O 与 CPU 混合，8 足够）
    "CSV": "pdf_check_report.csv",        # 相对路径按脚本所在目录解析；空=不写
    "LIST_FILE": "",        # 损坏文件纯路径清单（便于 xargs/PowerShell 批量搬运）
    "TAIL_WIN": 1024,       # 模拟严格阅读器的尾部回搜窗口（字节）
    "MAX_FULL_READ": 64 * 1024 * 1024,    # 超过此大小只做局部分段读，不整文件进内存
    "CLI_TIMEOUT": 60,      # 外部 qpdf/pdfinfo/mutool 单文件超时（秒）
}

_MIB = 1024 * 1024
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 我们这条管线认得的产物后缀（默认也一起检测，可用 --exclude-artifact 排除）
_ARTIFACT_SUFFIXES = ("_sssss", "_smallsmall", "_s")
_PAD_MARK = b"%inflate-begin("        # 旧/新填充块共同存在的起始标记
_PAD_END = b"%inflate-end"            # 填充块结束标记（紧邻最后的 %%EOF）

_lock = threading.Lock()
_stats = {
    "total": 0, "ok": 0, "warn": 0, "broken": 0, "read_err": 0,
    "by_code": {}, "t_parse": 0.0, "bytes": 0,
}
_printed = 0    # 已输出的结果行数（用于决定进度条刷新节奏）


# ---------------- 控制台编码兼容（Windows GBK / Linux LANG=C 都可能编不出中文与 ✓✗） ----------------

def _stream_needs_utf8(stream) -> bool:
    """当前编码能不能表示中文与 ✓✗；不能（如 LANG=C/POSIX 下的 ascii）则需切 UTF-8。"""
    enc = (getattr(stream, "encoding", "") or "").lower()
    if not enc:
        return True
    try:
        "中✓".encode(enc)
        return False
    except (LookupError, UnicodeEncodeError):
        return True


def ensure_utf8_streams():
    """只在原编码编不出中文时才切 UTF-8；否则保持原编码（Windows GBK 控制台才不会乱码）。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        switch = _stream_needs_utf8(stream)
        try:                                        # Py3.7+
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
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
        except Exception:
            pass


def log(msg: str):
    with _lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)


def human_size(num) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}TB"


def human_secs(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


# ---------------- 进度条（按文件个数刷新，线程安全；-q 关闭） ----------------

class Counter:
    def __init__(self, total: int, quiet: bool):
        self.total = max(0, int(total))
        self.done = 0
        self.start = time.time()
        self.quiet = quiet
        self.last = 0.0
        self._lock = threading.Lock()

    def tick(self):
        if self.quiet:
            return
        now = time.time()
        with self._lock:
            self.done += 1
            if now - self.last < 0.2 and self.done < self.total:
                return
            self.last = now
            pct = (self.done * 100.0 / self.total) if self.total else 0.0
            el = now - self.start
            eta = (el / self.done) * (self.total - self.done) if self.done and self.total else 0.0
            bar = "#" * int(pct / 4)
            with _lock:
                sys.stdout.write(
                    f"\r[{bar:<25}][{pct:5.1f}%] {self.done}/{self.total} "
                    f"损坏{_stats['broken']} 可疑{_stats['warn']} "
                    f"已用{human_secs(el)} 预计剩{human_secs(eta)}   ")
                sys.stdout.flush()

    def close(self):
        if self.quiet:
            return
        with _lock:
            sys.stdout.write(" " * 90 + "\r")
            sys.stdout.flush()


# ---------------- 局部读取封装（大文件不整份进内存） ----------------

class LazyFile:
    """只做「头部/尾部/按偏移取一段」的随机读，避免 4 万个文件时内存爆掉。"""

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)
        self.f = open(path, "rb")

    def head(self, n: int = 1024) -> bytes:
        self.f.seek(0)
        return self.f.read(min(n, self.size))

    def tail(self, n: int = 4096) -> bytes:
        off = max(0, self.size - min(n, self.size))
        self.f.seek(off)
        return self.f.read(self.size - off)

    def pread(self, off: int, n: int) -> bytes:
        if off < 0 or off >= self.size:
            return b""
        self.f.seek(off)
        return self.f.read(min(n, self.size - off))

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------- 结构解析小工具 ----------------

_OBJ_RE_TMPL = rb"(?<![\d~>\w])%d\s+%d\s+obj\b"
_DICT_NEST_RE = re.compile(rb"<<|>>")


def find_dict_span(chunk: bytes, start: int):
    """从 chunk[start:] 里找到第一个完整的 << ... >> 字典（支持嵌套），返回 (文本, 结束下标)。"""
    i = chunk.find(b"<<", start)
    if i < 0:
        return b"", -1
    depth = 0
    pos = i
    n = len(chunk)
    while pos < n:
        if chunk.startswith(b"<<", pos):
            depth += 1
            pos += 2
        elif chunk.startswith(b">>", pos):
            depth -= 1
            pos += 2
            if depth == 0:
                return chunk[i:pos], pos
        else:
            pos += 1
    return chunk[i:], -1            # 没闭合（文件被截断的典型征兆）


def dict_get_int(d: bytes, key: bytes):
    m = re.search(key + rb"\s+(-?\d+)", d)
    return int(m.group(1)) if m else None


def dict_get_ref(d: bytes, key: bytes):
    """取 /Key n 0 R 形式的引用，返回 (num, gen)。"""
    m = re.search(key + rb"\s+(\d+)\s+(\d+)\s+R\b", d)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_int_array(d: bytes, key: bytes):
    """取 /W [1 4 2] 这类整数数组。"""
    m = re.search(key + rb"\s*\[([^\]]*)\]", d)
    if not m:
        return None
    try:
        return [int(x) for x in m.group(1).split()]
    except ValueError:
        return None


def apply_predictor(buf: bytes, columns: int, predictor: int) -> bytes:
    """FlateDecode 的 TIFF/PNG 预测还原（xref 流带 /Predictor 时行前有 1 字节类型）。"""
    if predictor <= 1 or columns <= 0:
        return buf
    row_len = columns + 1
    rows = len(buf) // row_len
    out = bytearray(rows * columns)
    prev = bytearray(columns)
    bpp = 1                                    # xref 流按字节对齐，灰度像素宽取 1
    for r in range(rows):
        base = r * row_len
        cur = bytearray(buf[base + 1:base + 1 + columns])
        if predictor == 2:                      # TIFF：按 bpp 隔位左预测
            for i in range(bpp, columns):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif predictor >= 10:                   # PNG 0..4
            pt = buf[base]
            for i in range(columns):
                a = cur[i - 1] if (pt in (1, 2, 3, 4) and i) else 0
                b = prev[i]
                c = prev[i - 1] if (pt in (3, 4) and i) else 0
                if pt == 0:
                    v = cur[i]
                elif pt == 1:
                    v = cur[i] + a
                elif pt == 2:
                    v = cur[i] + b
                elif pt == 3:
                    v = cur[i] + (a + b) // 2
                elif pt == 4:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    v = cur[i] + pr
                else:
                    v = cur[i]
                cur[i] = v & 0xFF
        out[r * columns:(r + 1) * columns] = cur
        prev = cur
    return bytes(out)


def decode_stream(lf: LazyFile, obj_off: int):
    """读取 '<n> g obj' 处的对象字典与已解码流内容。返回 (dict, data, err)。"""
    chunk = lf.pread(obj_off, 16384)
    m = re.match(rb"\s*\d+\s+\d+\s+obj\b", chunk)
    if not m:
        return b"", b"", "对象头不符"
    dtext, dend = find_dict_span(chunk, m.end())
    if dend < 0:
        return dtext, b"", "字典未闭合"
    sidx = chunk.find(b"stream", dend)
    if sidx < 0:
        return dtext, b"", ""                          # 非流对象（正常，字典够用）
    eol = chunk.find(b"\n", sidx)
    raw_from = eol + 1 if eol >= 0 else sidx + 6
    need = dict_get_int(dtext, b"/Length")
    endi = chunk.find(b"endstream", raw_from)
    if need and need > 0:
        raw = lf.pread(obj_off + raw_from, need)
    elif endi > raw_from:
        raw = lf.pread(obj_off + raw_from, endi - raw_from)
    else:
        raw = lf.pread(obj_off + raw_from, min(64 * 1024, lf.size - obj_off - raw_from))
    filt = dtext.replace(b" ", b"")
    if b"/FlateDecode" in filt or b"/Filter</" in filt:
        dobj = b""
        mdi = filt.find(b"/DecodeParms")
        if mdi >= 0:
            dobj, _ = find_dict_span(dtext, mdi)
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = zlib.decompressobj(-15).decompress(raw) if b"/PNG" not in filt else None
            if data is None:
                return dtext, b"", "Flate 解压失败"
        pred = dict_get_int(dobj, b"/Predictor") or 1
        cols = dict_get_int(dobj, b"/Columns") or 0
        if pred > 1:
            data = apply_predictor(data, cols, pred)
        return dtext, data, ""
    return dtext, raw, ""


def locate_obj(lf: LazyFile, num: int, gen: int = 0):
    """全文件回搜 “<num> <gen> obj”。只在 xref 偏移不可信时才走到这里，成本可控。"""
    pat = re.compile(_OBJ_RE_TMPL % (num, gen))
    step = 8 * 1024 * 1024
    pos = 0
    prev_tail = b""
    while pos < lf.size:
        buf = lf.pread(pos, step + 32)
        if not buf:
            break
        mm = pat.search(buf)
        if mm:
            return pos + mm.start()
        if prev_tail:                                   # 上一块尾部与本块开头拼接处再找一次
            joined = prev_tail + buf[:32]
            m2 = pat.search(joined)
            if m2 and m2.start() < len(prev_tail):
                return pos - len(prev_tail) + m2.start()
        prev_tail = buf[-32:]
        pos += step
    return None


def check_obj_at(lf: LazyFile, off: int, num: int) -> bool:
    """xref 声称的偏移 off 处是否真的能看到 “num G obj”（容忍前后空白/偏移小偏差）。"""
    if not (0 < off < lf.size):
        return False
    seg = lf.pread(max(0, off - 8), 96)
    return bool(re.search(rb"(?<![\d~>\w])%d\s+\d+\s+obj\b" % num, seg))



# ---------------- 传统 xref 表 / 交叉引用流 ----------------

ENTRY_RE = re.compile(rb"(\d{10}) (\d{5}) ([nf])[\r\n ]{1,2}")


def parse_classic_xref(lf: LazyFile, off: int):
    """解析一段传统 xref 表。返回 ({objnum: offset}, trailer字典, /Prev 或 None)；失败返回 None。"""
    head = lf.pread(off, 48)
    m = re.match(rb"\s*xref\s+(\d+)\s+(\d+)", head)
    if not m:
        return None
    start, count = int(m.group(1)), int(m.group(2))
    pos = off + m.end()
    nl = lf.pread(pos, 2)
    pos += len(nl) - len(nl.lstrip(b"\r\n"))
    out, objnum = {}, start
    seen = 0
    while seen < count and seen < 400000:
        take = min(count - seen, 4096)
        blk = lf.pread(pos, take * 21 + 4)
        matched = list(ENTRY_RE.finditer(blk))
        if not matched:
            break
        for mm in matched:
            if mm.group(3) == b"n":
                out.setdefault(objnum, int(mm.group(1)))
            objnum += 1
        seen += len(matched)
        pos += matched[-1].end()
    tail_blk = lf.pread(pos, 8192)
    tm = re.search(rb"\btrailer\b", tail_blk)
    dtext = b""
    if tm:
        dtext, _ = find_dict_span(tail_blk, tm.end())
    prev = dict_get_int(dtext, b"/Prev")
    return out, dtext, prev


def _field(row: bytes, start: int, width: int, default: int = 0) -> int:
    if width <= 0:
        return default
    v = 0
    for b in row[start:start + width]:
        v = (v << 8) | b
    return v


def decode_xref_stream(dtext: bytes, data: bytes, fsize: int):
    """按 /W /Index /Size 还原交叉引用流条目。
       返回 ({objnum: 字节偏移}, {objnum: (所在对象流号, 序内下标)}, err)。
       type=1 是普通对象偏移；type=2 是压缩在对象流(ObjStm)里的对象，两者都得记。"""
    W = parse_int_array(dtext, b"/W")
    if not W or len(W) < 3:
        return None, None, "缺 /W"
    idx = parse_int_array(dtext, b"/Index")
    if not idx or len(idx) < 2:
        size = dict_get_int(dtext, b"/Size")
        idx = [0, size or 0]
    rowlen = sum(W)
    if rowlen <= 0 or not data:
        return None, None, "条目长度为 0"
    out, comp, pos = {}, {}, 0
    for si in range(0, len(idx) - 1, 2):
        base, cnt = idx[si], idx[si + 1]
        for k in range(cnt):
            if pos + rowlen > len(data):
                break
            row = data[pos:pos + rowlen]
            pos += rowlen
            typ = _field(row, 0, W[0], 1)
            f2 = _field(row, W[0], W[1])
            f3 = _field(row, W[0] + W[1], W[2])
            objnum = base + k
            if typ == 1 and 0 < f2 < fsize:
                out.setdefault(objnum, f2)
            elif typ == 2:
                comp.setdefault(objnum, (f2, f3))
        if pos + rowlen > len(data):
            break
    return out, comp, None


class ObjStmReader:
    """对象流（/Type/ObjStm）里的对象没有“N G obj”标记，只能从流内容里按偏移取字典。"""

    def __init__(self, lf: LazyFile, offsets: dict):
        self.lf = lf
        self.offsets = offsets
        self.cache = {}

    def _load(self, streamnum):
        if streamnum in self.cache:
            return self.cache[streamnum]
        rec = None
        why = ""
        off = self.offsets.get(streamnum)
        if off is None or not 0 < off < self.lf.size:
            off = locate_obj(self.lf, streamnum, 0)
            why = "定位失败" if off is None else "靠全文件回搜定位"
        if off is not None:
            dtext, data, err = decode_stream(self.lf, off)
            flat = dtext.replace(b" ", b"").replace(b"\r", b"").replace(b"\n", b"")
            if err:
                why = "流解压失败：" + err
            elif not data:
                why = "流内容为空"
            elif b"/ObjStm" not in flat:
                why = "目标对象不是 /ObjStm"
            else:
                n = dict_get_int(dtext, b"/N")
                first = dict_get_int(dtext, b"/First")
                if not n or first is None or first > len(data):
                    why = f"/N=/First 不合理（N={n} First={first} 流长={len(data)}）"
                else:
                    nums = [int(x) for x in re.findall(rb"\d+", data[:first])]
                    by_num, by_idx = {}, []
                    for i in range(min(n, len(nums) // 2)):
                        pos = first + nums[2 * i + 1]
                        by_num[nums[2 * i]] = pos
                        by_idx.append(pos)
                    rec = (by_num, by_idx, data)
        self.cache[streamnum] = (rec, why)
        return self.cache[streamnum]

    def dict_of(self, streamnum, index):
        """从对象流里取第 index 个对象（type=2 条目第三字段是序内下标，非对象号）。"""
        rec, why = self._load(streamnum)
        if not rec:
            return b"", f"对象流 {streamnum} 读不出（{why or '原因未知'}）"
        by_num, by_idx, data = rec
        pos = by_idx[index] if 0 <= index < len(by_idx) else by_num.get(index)
        if pos is None or pos >= len(data):
            return b"", f"对象流 {streamnum} 里取不出下标 {index}（共 {len(by_idx)} 个）"
        dtext, _ = find_dict_span(data, pos)
        if not dtext:
            return b"", f"对象流 {streamnum} 下标 {index} 处读不出字典"
        return dtext, ""


def parse_xref_chain(lf: LazyFile, first_off: int):
    """沿 /Prev 回溯整条 xref 链。返回 (字节偏移表, 压缩对象表, trailer字典, err)。"""
    offsets, comp, trailer, off, hops = {}, {}, b"", first_off, 0
    while off is not None and hops < 32:
        hops += 1
        if off <= 0 or off >= lf.size:
            return offsets, comp, trailer, "startxref 偏移越界"
        seg = lf.pread(off, 48)
        if re.match(rb"\s*xref\b", seg):
            res = parse_classic_xref(lf, off)
            if not res:
                return offsets, comp, trailer, "传统 xref 表格式不符"
            ents, dtext, prev = res
            for num, o in ents.items():
                offsets.setdefault(num, o)
            if not trailer:
                trailer = dtext
            off = prev
            continue
        m = re.match(rb"\s*(\d+)\s+(\d+)\s+obj\b", seg)
        if not m:
            return offsets, comp, trailer, "startxref 落点既不是 xref 表也不是对象"
        dtext, data, err = decode_stream(lf, off)
        if err:
            return offsets, comp, trailer, "xref 流读取失败：" + err
        flat = dtext.replace(b" ", b"").replace(b"\r", b"").replace(b"\n", b"")
        if b"/XRef" in flat and data:
            ents, comps, derr = decode_xref_stream(dtext, data, lf.size)
            if derr:
                return offsets, comp, trailer, "交叉引用流解码失败：" + derr
            for num, o in ents.items():
                offsets.setdefault(num, o)
            for num, c in comps.items():
                comp.setdefault(num, c)
            if not trailer:
                trailer = dtext
        elif not trailer:
            trailer = dtext
        off = dict_get_int(dtext, b"/Prev")
    return offsets, comp, trailer, None


def validate_offsets(lf: LazyFile, offsets: dict):
    """抽样校验“xref 声称的偏移处真的有对应对象”。返回 (抽样数, 失败数)。"""
    items = sorted(offsets.items())
    if not items:
        return 0, 0
    picks = items if len(items) <= 12 else [items[int(i * (len(items) - 1) / 11)] for i in range(12)]
    bad = sum(1 for num, off in picks if not check_obj_at(lf, off, num))
    return len(picks), bad


def resolve_dict(lf: LazyFile, ref, offsets, comp=None, stm=None):
    """按引用取对象字典。优先用 xref 偏移，其次查压缩对象流，最后才全文件回搜。
       返回 (字典, 失败说明)。"""
    num, gen = ref
    off = offsets.get(num)
    if off is not None and check_obj_at(lf, off, num):
        dtext, _, _ = decode_stream(lf, off)
        if dtext:
            return dtext, ""
    if comp and num in comp and stm is not None:
        streamnum, index = comp[num]
        dtext, snote = stm.dict_of(streamnum, index)
        if dtext:
            return dtext, ""
        return b"", snote or f"对象 {num} 在对象流 {streamnum} 中取不出"
    off2 = locate_obj(lf, num, gen)
    if off2 is None:
        return b"", f"对象 {num} {gen} obj 在文件里找不到"
    dtext, _, _ = decode_stream(lf, off2)
    return dtext, ""


def read_pages(lf: LazyFile, trailer: bytes, offsets: dict, comp=None, stm=None):
    """返回 (页数或 None, 说明)。拿不到时不算损坏（部分阅读器也能正常显示）。"""
    root_ref = dict_get_ref(trailer, b"/Root")
    if not root_ref:
        return None, "trailer 无 /Root"
    cat, cnote = resolve_dict(lf, root_ref, offsets, comp, stm)
    if not cat:
        return None, "/Root 指向的对象在文件里找不到（" + cnote + "）"
    if b"/Type/Catalog" not in cat.replace(b" ", b""):
        return None, "Root 对象缺 /Type/Catalog"
    pages_ref = dict_get_ref(cat, b"/Pages")
    if not pages_ref:
        return None, "Catalog 无 /Pages"
    pdict, pnote = resolve_dict(lf, pages_ref, offsets, comp, stm)
    if not pdict:
        return None, "/Pages 指向的对象在文件里找不到（" + pnote + "）"
    cnt = dict_get_int(pdict, b"/Count")
    if cnt and cnt > 0:
        return cnt, ""
    kids = re.search(rb"/Kids\s*\[([^\]]*)\]", pdict)
    if kids:
        n = len(re.findall(rb"\d+\s+\d+\s+R", kids.group(1)))
        if n:
            return n, "/Count 缺失，按 /Kids 数量估计"
    return None, "/Pages 无可用 /Count"


# ---------------- 尾部特征（重点：区分新旧填充产物） ----------------

def tail_check(lf: LazyFile, win: int) -> dict:
    t = lf.tail(max(win, 4096))
    w = lf.pread(max(0, lf.size - win), win)
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", w, re.DOTALL)
    m2 = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", t, re.DOTALL)
    return {
        "eof_ok": t.rstrip().endswith(b"%%EOF"),
        "pad_end": _PAD_END in t[-256:],
        "padded": _PAD_MARK in t or _PAD_END in t,
        "win_off": int(m.group(1)) if m else None,        # 尾部窗口内可寻的 xref 偏移
        "tail_off": int(m2.group(1)) if m2 else None,     # 稍大尾部（4KB+）里的
    }


def find_last_startxref(lf: LazyFile, limit: int = 96 * 1024 * 1024):
    """从文件尾向前分块找最后一个 startxref 的字节位置（只在可疑文件上调用）。"""
    step = 4 * 1024 * 1024
    scanned = 0
    while scanned < min(limit, lf.size):
        back = min(step, lf.size - scanned)
        base = lf.size - scanned - back
        buf = lf.pread(base, back + 32)
        idx = buf.rfind(b"startxref")
        if idx >= 0:
            return base + idx
        scanned += back
    return None


_MAGIC_HINT = [
    (b"PK\x03\x04", "zip/docx/jar"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG", "PNG"),
    (b"<html", "HTML"),
    (b"<!DOCTYPE", "HTML"),
    (b"\x7fELF", "可执行文件"),
    (b"Rar!", "RAR"),
    (b"%TIF", "未压缩的 TIFF"),
    (b"GIF8", "GIF"),
]


def sniff_non_pdf(lf: LazyFile) -> str:
    head = lf.head(64).lstrip()
    for magic, name in _MAGIC_HINT:
        if head.startswith(magic):
            return name
    if head[:1] in (b"<",):
        return "XML/HTML"
    return "未知内容"


# ---------------- 单文件体检 ----------------

BROKEN = "broken"
WARN = "warn"
OK = "ok"


def inspect(path: str, win: int):
    """返回 dict：code/level/pages/notes。只读，不做任何写入。"""
    res = {"path": path, "code": "OK", "level": OK, "pages": None, "notes": [], "size": 0}
    notes = res["notes"]
    try:
        res["size"] = os.path.getsize(path)
    except OSError as e:
        res.update(code="READ_ERROR", level=BROKEN)
        notes.append(f"取文件大小失败：{e}")
        return res
    if res["size"] == 0:
        res.update(code="EMPTY", level=BROKEN)
        notes.append("0 字节")
        return res
    try:
        with LazyFile(path) as lf:
            # 1) 文件头
            head = lf.head(4096)
            hv = re.search(rb"%PDF-(\d\.\d)", head)
            if not hv:
                probe = lf.head(64 * 1024)
                if probe and probe.count(0) > len(probe) * 0.9:
                    res.update(code="NUL_FILE", level=BROKEN)
                    notes.append(f"前 {len(probe) // 1024}KB 里 {probe.count(0) * 100 // len(probe)}% 是 0x00 字节："
                                 "占位/写入失败的空壳，从未真正写入 PDF 数据（只能回源重取）")
                    return res
                res.update(code="NOT_PDF", level=BROKEN)
                notes.append(f"头部 4KB 内无 %PDF- 版本号，实际内容像 {sniff_non_pdf(lf)}")
                return res
            res["version"] = hv.group(1).decode()
            if hv.start():
                notes.append(f"%PDF- 头前多了 {hv.start()} 字节（不合规但一般不影响阅读）")
            if lf.size < 1024:
                res.update(code="EMPTY", level=BROKEN)
                notes.append(f"仅 {lf.size} 字节，不可能含完整 PDF 结构")
                return res

            # 2) 尾部特征
            tc = tail_check(lf, win)
            if not tc["eof_ok"] and b"%%EOF" not in lf.tail(8192):
                res.update(code="NO_TRAILER_EOF", level=BROKEN)
                notes.append(f"尾部 8KB 内找不到 %%EOF（末尾字节 {lf.tail(16)!r}），文件被截断")
                return res

            # 3) startxref 定位
            xref_off = tc["win_off"] if tc["win_off"] is not None else tc["tail_off"]
            if xref_off is None:
                pos = find_last_startxref(lf)
                if pos is None:
                    res.update(code="NO_STARTXREF", level=BROKEN)
                    notes.append("全文件找不到 startxref")
                    return res
                m = re.match(rb"startxref\s+(\d+)", lf.pread(pos, 32))
                if not m:
                    res.update(code="NO_STARTXREF", level=BROKEN)
                    notes.append("startxref 后无合法偏移")
                    return res
                xref_off = int(m.group(1))
                dist = lf.size - pos
                if tc["padded"]:
                    # 我们的旧填充规则：几 MB 注释把 startxref 推出尾部回搜窗口
                    res["code"] = "PAD_LEGACY_TAIL"
                    res["level"] = WARN
                    notes.append(f"膨胀产物且尾部 {win}B 窗口内无 startxref（真身在距尾 "
                                 f"{human_size(dist)} 处）；MuPDF 等会报 cannot find startxref 并走修复分支")
                else:
                    res["code"] = "STRUCT_WARN"
                    res["level"] = WARN
                    notes.append(f"startxref 距文件尾 {human_size(dist)}，超出常规回搜窗口")

            # 4) xref 链与对象抽查
            offsets, comp, trailer, err = parse_xref_chain(lf, xref_off)
            stm = ObjStmReader(lf, offsets)
            if err:
                res["code"] = "XREF_DECODE_FAIL" if "流" in err or "解码" in err else "XREF_MISMATCH"
                res["level"] = BROKEN
                notes.append(err)
                if not offsets:
                    notes.append("一条 xref 条目都读不出，文件基本不可用（建议回源重取）")
                    return res
            if offsets:
                checked, bad = validate_offsets(lf, offsets)
                res["xref_entries"] = len(offsets)
                if bad and checked:
                    if bad == checked:
                        res["code"] = "XREF_MISMATCH"
                        res["level"] = BROKEN
                        notes.append(f"xref 抽样 {checked} 条偏移全部对不上对象（内容错位或截断）")
                    elif bad >= 3:
                        res["code"] = "XREF_MISMATCH"
                        res["level"] = BROKEN
                        notes.append(f"xref 抽样 {checked} 条中 {bad} 条偏移对不上对象")
                    else:
                        if res["level"] == OK:
                            res["code"], res["level"] = "STRUCT_WARN", WARN
                        notes.append(f"xref 抽样 {checked} 条中 {bad} 条偏移可疑")
                size_decl = dict_get_int(trailer, b"/Size")
                maxnum = max(offsets)
                # /Size 比最大对象号大是正常的（高位对象可能是空闲槽），小了就一定是谎报
                if size_decl and size_decl <= maxnum:
                    notes.append(f"trailer /Size={size_decl} 不大于实际最大对象号 {maxnum}（谎报，可疑）")
                    if res["level"] == OK:
                        res["code"], res["level"] = "STRUCT_WARN", WARN

            # 5) 加密不算损坏
            if b"/Encrypt" in trailer.replace(b" ", b""):
                res["code"] = "ENCRYPTED"
                res["level"] = WARN if res["level"] == OK else res["level"]
                notes.append("含 /Encrypt（需口令或已加密），结构层无法继续校验页面")
                return res

            # 6) 目录树与页数
            pages, pnote = read_pages(lf, trailer, offsets, comp, stm)
            res["pages"] = pages
            if pnote:
                if res["level"] == OK:
                    res["code"], res["level"] = ("NO_CATALOG", BROKEN) if "找不到" in pnote or "无 /Root" in pnote \
                        else ("STRUCT_WARN", WARN)
                    notes.append(pnote)
                else:
                    notes.append(pnote)
            elif pages == 0:
                res["code"], res["level"] = "PAGES_ZERO", BROKEN
                notes.append("页数解析为 0")

            # 7) 膨胀产物且一切正常 → 新规则产物
            if tc["padded"] and res["code"] == "OK":
                res["code"] = "OK_PADDED_NEW"
                notes.append("膨胀产物，尾部已补 startxref（新规则）")
            return res
    except Exception as e:                                   # 单个文件绝不拖垮整轮
        res.update(code="READ_ERROR", level=BROKEN)
        notes.append(f"解析异常：{type(e).__name__}: {e}")
        return res


# ---------------- 外部权威引擎交叉印证（装了才用） ----------------

CLI_TOOLS = ("qpdf", "pdfinfo", "mutool")


def detect_cli() -> dict:
    found = {}
    for tool in CLI_TOOLS:
        p = shutil.which(tool)
        if p:
            found[tool] = p
    return found


def _run(cmd, timeout):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, **kw)


def cli_check(tools: dict, path: str, timeout: int):
    """返回 (verdict, 摘要)。verdict ∈ ok/warn/broken/None。"""
    if not tools:
        return None, ""
    if "qpdf" in tools:
        try:
            r = _run([tools["qpdf"], "--check", "--no-warn", path], timeout)
            out = (r.stdout or b"").decode("utf-8", "replace")
            rc = r.returncode
            if rc == 0:
                return OK, "qpdf ok"
            if rc == 2:
                first = next((ln for ln in out.splitlines() if "WARNING" in ln), "qpdf warn")
                return WARN, first.strip()[:160]
            if rc == 3:
                first = next((ln for ln in out.splitlines() if "ERROR" in ln), "qpdf error")
                return BROKEN, first.strip()[:160]
            return BROKEN, (out.strip().splitlines() or ["qpdf 无法读取"]) [0][:160]
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"qpdf 调用失败：{e}"
    if "pdfinfo" in tools:
        try:
            r = _run([tools["pdfinfo"], path], timeout)
            out = (r.stdout or b"").decode("utf-8", "replace")
            if r.returncode == 0:
                m = re.search(r"^Pages:\s+(\d+)", out, re.M)
                return OK, f"pdfinfo ok（{('Pages=' + m.group(1)) if m else '无页数'}）"
            return BROKEN, (out.strip().splitlines() or ["pdfinfo 读取失败"])[-1][:160]
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"pdfinfo 调用失败：{e}"
    if "mutool" in tools:
        try:
            r = _run([tools["mutool"], "info", path], timeout)
            out = (r.stdout or b"").decode("utf-8", "replace")
            if r.returncode == 0:
                return OK, "mutool ok"
            return BROKEN, (out.strip().splitlines() or ["mutool 读取失败"])[-1][:160]
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"mutool 调用失败：{e}"
    return None, ""


def merge_cli(res: dict, verdict, summary: str):
    """内置结论与 CLI 结论互相印证：CLI 说坏而内置判 ok → 提级；内置说坏但 CLI 明确说 ok → 降级可疑。"""
    res["cli"] = verdict or ""
    res["cli_note"] = summary
    if not verdict:
        return
    if verdict == BROKEN and res["level"] == OK:
        res["level"] = BROKEN
        if res["code"] in ("OK", "OK_PADDED_NEW"):
            res["code"] = "CLI_BROKEN"
        res["notes"].append(f"仅外部引擎发现：{summary}")
    elif verdict == WARN and res["level"] == OK:
        res["level"] = WARN
        res["code"] = "STRUCT_WARN" if res["code"] == "OK" else res["code"]
        res["notes"].append(f"外部引擎告警：{summary}")
    elif verdict == OK and res["level"] == BROKEN and res["code"] not in ("EMPTY", "NOT_PDF", "READ_ERROR"):
        # 铁证类（空文件/非 PDF）不会被推翻，其余冲突降级，避免误报损坏
        res["level"] = WARN
        res["code"] = "CONFLICT"
        res["notes"].append(f"内置判坏但 {summary} 认为可读，降级为可疑，需人工确认")


# ---------------- 文件收集 ----------------

def is_artifact(name: str) -> bool:
    root = os.path.splitext(name)[0]
    return any(root.endswith(suf) for suf in _ARTIFACT_SUFFIXES)


def collect_files(dirs: list, exts: list, recursive: bool, exclude_artifact: bool,
                  skip_dirs: list) -> list:
    out, skipped = [], 0
    for d in dirs:
        if os.path.isfile(d):
            if d.lower().endswith(tuple(exts)):
                out.append(os.path.abspath(d))
            continue
        if not os.path.isdir(d):
            log(f"警告：目录不存在，已忽略：{d}")
            continue
        if recursive:
            for root, sub, files in os.walk(d):
                sub[:] = [s for s in sub if not s.startswith(".") and s not in skip_dirs]
                for fn in files:
                    if fn.lower().endswith(tuple(exts)):
                        if exclude_artifact and is_artifact(fn):
                            skipped += 1
                            continue
                        out.append(os.path.join(root, fn))
        else:
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp) and fn.lower().endswith(tuple(exts)):
                    if exclude_artifact and is_artifact(fn):
                        skipped += 1
                        continue
                    out.append(fp)
    return sorted(set(out)), skipped


# ---------------- 汇总输出 ----------------

LEVEL_ORDER = {BROKEN: 0, WARN: 1, OK: 2}


def resolve_out(raw: str, from_config: bool) -> str:
    """输出路径：命令行给的就按当前工作目录理解；CONFIG 里的默认值按脚本目录落地。"""
    if not raw:
        return ""
    return os.path.abspath(raw if not from_config or os.path.isabs(raw)
                           else os.path.join(_SCRIPT_DIR, raw))


def write_outputs(rows: list, csv_path: str, list_path: str, copy_dir: str):
    if csv_path:
        try:
            parent = os.path.dirname(csv_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                wr = csv.writer(f)
                wr.writerow(["结论码", "等级", "大小", "页数", "xref条目", "版本", "外部印证", "路径", "细节"])
                for r in sorted(rows, key=lambda x: (LEVEL_ORDER[x["level"]], -x["size"])):
                    wr.writerow([r["code"], r["level"], r["size"], r.get("pages") or "",
                                 r.get("xref_entries") or "", r.get("version") or "",
                                 f"{r.get('cli') or ''} {r.get('cli_note') or ''}".strip(),
                                 r["path"], "；".join(r["notes"])])
            log(f"报告已写出：{csv_path}（{len(rows)} 行，UTF-8 BOM，Excel 可直接打开）")
        except OSError as e:
            log(f"警告：写 CSV 失败：{e}")
    picked = [r for r in rows if r["level"] in (BROKEN, WARN)]
    if list_path:
        try:
            with open(list_path, "w", encoding="utf-8") as f:
                for r in sorted(picked, key=lambda x: (LEVEL_ORDER[x["level"]], x["path"])):
                    f.write(r["path"] + "\n")
            log(f"损坏/可疑清单：{list_path}（{len(picked)} 条）")
        except OSError as e:
            log(f"警告：写清单失败：{e}")
    if copy_dir:
        os.makedirs(copy_dir, exist_ok=True)
        n = 0
        for r in rows:
            if r["level"] != BROKEN:
                continue
            try:
                shutil.copy2(r["path"], os.path.join(copy_dir, os.path.basename(r["path"])))
                n += 1
            except OSError as e:
                log(f"警告：拷贝失败 {r['path']}：{e}")
        log(f"已把 {n} 个判定损坏的文件拷到：{copy_dir}（源文件未动）")


def print_summary(rows: list, secs: float, tools: dict, broken_only: bool):
    by_code, by_level = {}, {}
    for r in rows:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
        by_code.setdefault(r["code"], []).append(r)
    total = len(rows)
    print(f"\n===== 体检结果（共 {total} 个，用时 {human_secs(secs)}）=====")
    for lv, label in ((BROKEN, "损坏"), (WARN, "可疑"), (OK, "正常")):
        print(f"  {label}：{by_level.get(lv, 0)}")
    if total:
        print(f"  合计耗时：解析 {human_secs(_stats['t_parse'])}，"
              f"读取 {human_size(_stats['bytes'])}，"
              f"平均 {human_secs(secs / total)}/个")
    print("\n  按结论码：")
    rank = {BROKEN: 0, WARN: 1, OK: 2}
    for code, items in sorted(by_code.items(),
                              key=lambda kv: (rank.get(kv[1][0]["level"], 2), -len(kv[1]))):
        lv = items[0]["level"]
        mark = {BROKEN: "✗", WARN: "!", OK: "✓"}.get(lv, "?")
        print(f"   {mark} {code:<17} {len(items):>6} 个   例：{os.path.basename(items[0]['path'])[:40]}")
    print("\n  问题明细（正常文件不列出）：")
    shown = 0
    for r in sorted(rows, key=lambda x: (LEVEL_ORDER[x["level"]], -x["size"])):
        if r["level"] == OK:
            continue
        if broken_only and r["level"] != BROKEN:
            continue
        extra = f" 页数 {r['pages']}" if r.get("pages") else ""
        cli = f" | 外部：{r.get('cli_note')}" if r.get("cli_note") else ""
        print(f"   [{r['level']:<6}]{r['code']:<17}{human_size(r['size']):>9}{extra}  "
              f"{os.path.relpath(r['path'])}\n        {'；'.join(r['notes']) or '（无附加说明）'}{cli}")
        shown += 1
    if shown == 0:
        print("   （无）")
    tips = []
    if by_code.get("PAD_LEGACY_TAIL"):
        tips.append(f"旧填充产物 {len(by_code['PAD_LEGACY_TAIL'])} 个：用 pdf_rename_inflate_upload.py "
                    f"--refix --apply 重做即可（_sssss 里存的就是原件）")
    if by_code.get("XREF_MISMATCH") or by_code.get("NO_TRAILER_EOF"):
        tips.append("偏移错位/截断的那批基本是传输或写入被截，需回源重新下载")
    if by_code.get("NOT_PDF"):
        tips.append("非 PDF 内容的文件（多半是下载失败的错误页被存成 .pdf），可按清单删掉重取")
    if by_code.get("NUL_FILE"):
        tips.append(f"全 0x00 空壳 {len(by_code['NUL_FILE'])} 个：跟膨胀无关，是当时写入/上传根本没成功，只能回源重取")
    if by_code.get("ENCRYPTED"):
        tips.append("加密文件不是损坏，只是结构层读不动，别误删")
    if not tools:
        tips.append("未装 qpdf/pdfinfo/mutool，仅内置结构判定；要更权威可 apt install qpdf poppler-utils")
    if tips:
        print("\n  建议：")
        for t in tips:
            print(f"   - {t}")


# ---------------- 入口 ----------------

def main():
    ensure_utf8_streams()
    parser = argparse.ArgumentParser(
        description="本地 PDF 批量损坏体检（只读；内置结构校验 + 可选 qpdf/pdfinfo/mutool 交叉印证）")
    parser.add_argument("dirs", nargs="*", help=f"要检测的目录或文件（可多个）；默认 {CONFIG['DIR'] or '当前目录'}")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多检测多少个（0=全部）")
    parser.add_argument("--sample", type=int, default=0, help="随机抽样 N 个（4 万多份先摸底用；0=不抽样）")
    parser.add_argument("--seed", type=int, default=20261029, help="抽样随机种子（同种子结果可复现）")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help=f"并发线程数；不传用 CONFIG['WORKERS']（当前：{CONFIG['WORKERS']}）")
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示进度条")
    parser.add_argument("--show-all", action="store_true", help="正常文件也逐个打印（默认只打印可疑/损坏）")
    parser.add_argument("--broken-only", action="store_true", help="明细里只列损坏，不列可疑")
    parser.add_argument("--no-recursive", action="store_true", help="只扫目录第一层")
    parser.add_argument("--ext", default=".pdf,.PDF", help="要检测的扩展名，逗号分隔")
    parser.add_argument("--exclude-artifact", action="store_true",
                        help="跳过 _sssss/_smallsmall/_s 这些产物名")
    parser.add_argument("--no-cli", action="store_true", help="不调用外部 qpdf/pdfinfo/mutool")
    parser.add_argument("--csv", nargs="?", const="", default=None,
                        help=f"写出 CSV 报告（跟路径；相对路径按脚本目录；只写 --csv 不带值=不写；"
                             f"当前默认：{CONFIG['CSV']}）")
    parser.add_argument("--list-file", nargs="?", const="", default=None,
                        help="把可疑+损坏的路径写成纯文本清单（只写参数不带值=不写）")
    parser.add_argument("--copy-broken", default=None, help="把判定损坏的文件拷到该目录（不删源）")
    parser.add_argument("--tail-window", type=int, default=None,
                        help=f"模拟严格阅读器的尾部回搜窗口字节数（当前：{CONFIG['TAIL_WIN']}）")
    args = parser.parse_args()

    dirs = args.dirs or ([CONFIG["DIR"]] if CONFIG["DIR"] else ["."])
    dirs = [d for d in dirs if d]
    exts = [e.strip().lower() for e in args.ext.split(",") if e.strip()]
    if not exts:
        parser.error("--ext 不能为空")
    workers = int(args.workers if args.workers is not None else CONFIG["WORKERS"])
    if workers < 1:
        parser.error("-w/--workers 需 >= 1")
    win = int(args.tail_window if args.tail_window is not None else CONFIG["TAIL_WIN"])

    raw_csv = str(CONFIG.get("CSV") or "") if args.csv is None else args.csv
    csv_path = resolve_out(raw_csv, args.csv is None)
    raw_list = str(CONFIG.get("LIST_FILE") or "") if args.list_file is None else args.list_file
    list_path = resolve_out(raw_list, args.list_file is None)
    copy_dir = resolve_out(args.copy_broken, False) if args.copy_broken else ""

    files, skipped = collect_files(dirs, exts, not args.no_recursive,
                                   args.exclude_artifact, ["node_modules", "dist", "__pycache__"])
    if not files:
        print("没有找到待检测的文件（目录为空、扩展名不符或都已被排除）")
        return 0
    if args.sample > 0 and args.sample < len(files):
        random.seed(args.seed)
        files = random.sample(files, args.sample)
    elif args.limit > 0:
        files = files[:args.limit]

    tools = {} if args.no_cli else detect_cli()
    print(f"待检测 {len(files)} 个文件（跳过产物名 {skipped} 个），并发 {workers} 线程")
    print(f"目录：{'、'.join(os.path.abspath(d) for d in dirs[:3])}{' …' if len(dirs) > 3 else ''}")
    print(f"外部印证引擎：{'、'.join(tools) if tools else '未检测到（只用内置结构校验）'}")
    if csv_path:
        print(f"CSV 报告：{csv_path}")
    print()

    t0 = time.time()
    prog = Counter(len(files), args.quiet)

    def work(path):
        t = time.time()
        res = inspect(path, win)
        with _lock:
            _stats["t_parse"] += time.time() - t
        if tools:
            verdict, summary = cli_check(tools, path, CONFIG["CLI_TIMEOUT"])
            merge_cli(res, verdict, summary)
        with _lock:
            _stats["total"] += 1
            _stats[res["level"] if res["level"] != OK else "ok"] += 1
            _stats["by_code"][res["code"]] = _stats["by_code"].get(res["code"], 0) + 1
            _stats["bytes"] += res["size"]
        prog.tick()
        return res

    workers = max(1, min(workers, len(files)))
    rows = []
    try:
        if workers > 1:
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chk")
            try:
                rows = list(pool.map(work, files))
            except KeyboardInterrupt:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
                raise
            finally:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
        else:
            rows = [work(p) for p in files]
    except KeyboardInterrupt:
        log("\n[中断] 已停止；以下为已完成部分的统计：")
    finally:
        prog.close()

    secs = time.time() - t0
    if args.show_all:
        for r in sorted(rows, key=lambda x: x["path"]):
            print(f"   [ok    ]{r['code']:<17}{human_size(r['size']):>9} {os.path.relpath(r['path'])}")
    print_summary(rows, secs, tools, args.broken_only)
    write_outputs(rows, csv_path, list_path, copy_dir)
    return 1 if _stats["broken"] else 0


if __name__ == "__main__":
    sys.exit(main())
