#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_inflate.py —— 让 PDF 文件变大的脚本（零依赖，仅用标准库）

原理：
  PDF 规范允许在 %%EOF 之后存在尾部数据（阅读器通过 StartXRef 定位交叉引用表，
  会忽略其后的多余字节）。本脚本向 PDF 末尾追加一段合法的注释包裹的填充数据，
  既能让文件显著变大，又不影响正常打开、渲染与解析。

用法：
  python pdf_inflate.py input.pdf                       # 默认膨胀为原文件的 2 倍
  python pdf_inflate.py input.pdf -x 5                  # 膨胀为原文件的 5 倍
  python pdf_inflate.py input.pdf -s 50MB               # 目标大小约 50MB（支持 KB/MB/GB）
  python pdf_inflate.py input.pdf -p 20MB               # 固定追加 20MB 填充
  python pdf_inflate.py input.pdf -o out.pdf            # 另存为新文件（默认 *_inflated.pdf）
  python pdf_inflate.py *.pdf -x 3                      # 支持批量处理（Windows 下通配符由脚本自行展开）
  python pdf_inflate.py huge.pdf -q                     # 静默模式，不输出进度条

说明：
  - 默认不覆盖原文件，输出为 <原名>_inflated.pdf。
  - 追加的数据以 "%inflate(...)" 注释形式包裹，属于 PDF 语法中的合法注释，
    删除尾部数据即可还原（近似），不会破坏原对象结构。
  - 处理过程会显示进度条（阶段/百分比/已写与总量/速度/预计剩余/已用时间），
    结束后输出每个文件及整体的耗时与平均速度；输出被重定向到文件时进度条自动静音。
"""

import argparse
import glob
import os
import re
import shutil
import sys
import time
import unicodedata

# 每次写入的块大小（8MB），避免一次性构造超大 bytes 占内存
_CHUNK = 8 * 1024 * 1024
# 填充用的重复字节（随意，可打印字符便于识别）
_FILL_BYTE = b"p"
# 填充块结束标记
_PAD_FOOTER = b"%inflate-end\n%%EOF\n"


def parse_size(text: str) -> int:
    """解析 '50MB'、'1.5GB'、'2000' 等形式为字节数。"""
    text = text.strip().upper().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(B|KB|MB|GB|TB)?", text)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析大小: {text!r}（示例: 500KB / 20MB / 1.5GB）")
    num = float(m.group(1))
    unit = m.group(2) or "B"
    multiplier = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(num * multiplier)


def human_size(num) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}TB"


def human_seconds(sec: float) -> str:
    """将秒数格式化为人可读时长：1.2s / 3m04.5s / 1h02m03s。"""
    sec = max(float(sec), 0.0)
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"{int(m)}m{s:04.1f}s"
    m, s = divmod(int(sec - 3600 * (sec // 3600)), 60)
    return f"{int(sec // 3600)}h{m:02d}m{s:02d}s"


def _disp_width(text: str) -> int:
    """计算字符串在终端里的列宽（中日韩全角字符算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _truncate(text: str, limit: int) -> str:
    """按终端列宽截断，超出部分用省略号代替。"""
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
    """零依赖进度条：TTY 下原地刷新，输出重定向（管道/写文件）时自动静音。"""

    _REFRESH = 0.1  # 刷新间隔（秒），避免高频写终端拖慢IO

    def __init__(self, label: str, total: int = 0, quiet: bool = False, stream=None):
        self.stream = stream or sys.stderr
        self.label = label
        self.total = max(int(total), 0)
        self.done = 0
        self.stage = ""
        self.t0 = time.time()
        self._last = 0.0
        self._last_w = 0  # 上一帧的列宽，用于定点清除
        self._cols = shutil.get_terminal_size((100, 24)).columns
        self.enabled = (not quiet) and hasattr(self.stream, "isatty") and self.stream.isatty()

    def set_stage(self, name: str):
        if name != self.stage:
            self.stage = name
            self._render(force=True)

    def update(self, nbytes: int):
        self.done += nbytes
        self._render()

    def close(self):
        if self.enabled:
            self._render(force=True)
            self.stream.write("\n")
            self.stream.flush()

    def _render(self, force: bool = False):
        if not self.enabled:
            return
        now = time.time()
        if not force and (now - self._last < self._REFRESH and self.done < self.total):
            return
        self._last = now

        elapsed = max(now - self.t0, 1e-6)
        ratio = min(self.done / self.total, 1.0) if self.total > 0 else 0.0
        rate = self.done / elapsed
        remain = (self.total - self.done) / rate if rate > 0 else 0.0

        head = f"[{_truncate(self.label, max(self._cols // 5, 8))}] "
        pct = f"{ratio * 100:5.1f}%"
        # 可舍弃字段按重要度从低到高排列；百分比与已用时间始终保留
        parts = [self.stage, f"{human_size(self.done)}/{human_size(self.total)}",
                 f"{human_size(rate)}/s", f"剩 {human_seconds(remain)}"]
        kept = f"用 {human_seconds(elapsed)}"

        bar_w, body = 0, ""
        for drop in range(len(parts) + 1):
            body = "  ".join(x for x in ([pct] + parts[drop:] + [kept]) if x)
            avail = self._cols - _disp_width(head) - _disp_width(body) - 4
            bar_w = min(max(avail, 0), 40)
            if bar_w >= 8 or drop == len(parts):
                break
        if bar_w >= 8:
            filled = int(bar_w * ratio)
            line = f"{head}|{'█' * filled}{'░' * (bar_w - filled)}| {body}"
        else:
            line = f"{head}{body}"  # 空间极少：只留文字，不画条
        line = _truncate(line, self._cols)
        w = _disp_width(line)
        self.stream.write("\r" + line)
        # 只清除上一帧多出的字符，避免写入大片尾随空白
        self.stream.write(" " * max(self._last_w - w, 0))
        self._last_w = w
        self.stream.flush()


class _CountedWriter:
    """文件对象的轻包装：写入同时回报字节数，便于统计进度。"""

    def __init__(self, fh, progress: Progress = None):
        self._fh = fh
        self._progress = progress

    def write(self, data: bytes):
        self._fh.write(data)
        if self._progress is not None:
            self._progress.update(len(data))

    def flush(self):
        self._fh.flush()


def is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def write_padding(stream, nbytes: int):
    """以注释块形式向 PDF 尾部写入 nbytes 填充数据（开头自带换行，避免与 %%EOF 粘连）。

    实际写入总量（含结束标记）恒为 nbytes + len(_PAD_FOOTER)，便于精确预估进度。
    """
    if nbytes <= 0:
        return
    # 注释开头标记
    header = b"\n%inflate-begin(" + str(nbytes).encode() + b")\n"
    stream.write(header)
    remaining = nbytes - len(header)
    while remaining > 0:
        size = min(_CHUNK, remaining)
        if size >= 2:
            # 1 字节 % + (size-2) 填充 + 1 字节换行 = 恰好 size 字节
            stream.write(b"%" + _FILL_BYTE * (size - 2) + b"\n")
        else:  # 只剩 1 字节：用换行补齐，保证总量准确
            stream.write(b"\n")
        remaining -= size
    stream.write(_PAD_FOOTER)


def inflate_one(src: str, dst: str, target_size: int = None, factor: float = None, pad: int = None,
                progress: Progress = None):
    """膨胀单个文件；成功返回统计信息 dict，跳过/失败返回 None。"""
    src_size = os.path.getsize(src)
    if not is_pdf(src):
        print(f"[跳过] 不是 PDF 文件: {src}", file=sys.stderr)
        return None

    # 计算需要追加的填充字节数
    if pad is not None:
        add = pad
    elif target_size is not None:
        add = max(0, target_size - src_size)
    else:  # 默认 2 倍
        add = int(src_size * (factor - 1))

    if add <= 0:
        print(f"[跳过] 目标大小不超过原文件({human_size(src_size)}): {src}", file=sys.stderr)
        return None

    # 预期总大小 = 原文件 + 填充体 + 结束标记，用于百分比与 ETA
    if progress is not None:
        progress.total = src_size + add + len(_PAD_FOOTER)
        progress.done = 0
        progress.t0 = time.time()

    t0 = time.time()
    try:
        with open(src, "rb") as fin, open(dst, "wb") as raw:
            out = _CountedWriter(raw, progress)
            if progress is not None:
                progress.set_stage("复制原文件")
            while True:
                chunk = fin.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
            if progress is not None:
                progress.set_stage("追加填充")
            write_padding(out, add)
            raw.flush()
    except BaseException:
        # 出错或 Ctrl+C 中断时清理半成品，避免留下不完整的巨大文件
        if progress is not None:
            progress.close()
        try:
            if os.path.isfile(dst):
                os.remove(dst)
        except OSError:
            pass
        raise

    secs = time.time() - t0
    dst_size = os.path.getsize(dst)
    if progress is not None:
        progress.close()

    rate = dst_size / secs if secs > 0 else 0
    print(f"[完成] {src} ({human_size(src_size)}) -> {dst} ({human_size(dst_size)})，"
          f"追加约 {human_size(add)}，耗时 {human_seconds(secs)}，平均 {human_size(rate)}/s")
    return {"src": src, "dst": dst, "src_size": src_size, "dst_size": dst_size, "add": add, "secs": secs}


def main():
    parser = argparse.ArgumentParser(
        description="让 PDF 文件变大（追加合法尾部填充数据，不影响打开）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python pdf_inflate.py demo.pdf\n"
               "  python pdf_inflate.py demo.pdf -x 5\n"
               "  python pdf_inflate.py demo.pdf -s 50MB\n"
               "  python pdf_inflate.py demo.pdf -p 20MB -o big.pdf\n"
               "  python pdf_inflate.py *.pdf -x 50 -q\n",
    )
    parser.add_argument("inputs", nargs="+", help="输入 PDF 文件（可多个，支持通配符）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-x", "--factor", type=float, help="膨胀倍数（默认 2 倍），如 -x 5")
    group.add_argument("-s", "--size", type=parse_size, help="目标文件大小，如 -s 50MB")
    group.add_argument("-p", "--pad", type=parse_size, help="固定追加的填充大小，如 -p 20MB")
    parser.add_argument("-o", "--output", help="输出文件路径（仅单输入时有效；默认 <原名>_inflated.pdf）")
    parser.add_argument("--inplace", action="store_true", help="直接覆盖原文件（谨慎使用）")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默模式：不显示进度条")
    args = parser.parse_args()

    # Windows (cmd/PowerShell) 不会替程序展开通配符，这里自行展开，便于 *.pdf 批量用法
    inputs = []
    for pattern in args.inputs:
        if "*" in pattern or "?" in pattern:
            matched = sorted(glob.glob(pattern))
            # 批量时默认跳过已有的 *_inflated 产物，避免膨胀结果被二次膨胀
            matched = [p for p in matched if "_inflated" not in os.path.splitext(os.path.basename(p))[0]]
            if not matched:
                print(f"[跳过] 通配符无匹配: {pattern}", file=sys.stderr)
            inputs.extend(matched)
        else:
            inputs.append(pattern)
    if not inputs:
        parser.error("没有匹配到任何输入文件")

    if args.output and len(inputs) > 1:
        parser.error("-o/--output 仅支持单个输入文件")

    t_all = time.time()
    results = []
    failed = 0
    total_cnt = len(inputs)
    try:
        for idx, src in enumerate(inputs, 1):
            if not os.path.isfile(src):
                print(f"[跳过] 文件不存在: {src}", file=sys.stderr)
                failed += 1
                continue
            if args.inplace:
                dst = src
            elif args.output:
                dst = args.output
            else:
                root, ext = os.path.splitext(src)
                dst = f"{root}_inflated{ext or '.pdf'}"

            label = f"{idx}/{total_cnt} " + os.path.basename(src) if total_cnt > 1 else os.path.basename(src)
            progress = Progress(label, quiet=args.quiet)
            info = None
            try:
                if dst == src:
                    # 就地覆盖：先写临时文件再替换，避免中途失败损坏原文件
                    tmp = dst + ".tmp"
                    info = inflate_one(src, tmp, args.size, args.factor or 2.0, args.pad, progress)
                    if info:
                        os.replace(tmp, dst)
                        info["dst"] = dst
                else:
                    info = inflate_one(src, dst, args.size, args.factor or 2.0, args.pad, progress)
            finally:
                if progress.enabled and progress.done < progress.total:
                    progress.close()

            if info:
                results.append(info)
            else:
                failed += 1
    except KeyboardInterrupt:
        print("\n[中断] 已取消本次运行，已完成的文件保留在磁盘上", file=sys.stderr)
        _summary(results, failed, time.time() - t_all)
        sys.exit(130)

    _summary(results, failed, time.time() - t_all)
    if not results:
        sys.exit(1)


def _summary(results, failed: int, secs: float):
    """输出批量处理总览：个数、写盘总量、累计处理耗时与整体用时。"""
    if results:
        out_bytes = sum(r["dst_size"] for r in results)
        work_secs = sum(r["secs"] for r in results)
        total_add = sum(r["add"] for r in results)
        avg_rate = out_bytes / work_secs if work_secs > 0 else 0
        print(f"[汇总] 成功 {len(results)} 个，跳过/失败 {failed} 个；"
              f"写盘合计 {human_size(out_bytes)}（其中填充 {human_size(total_add)}），"
              f"处理耗时 {human_seconds(work_secs)}，整体用时 {human_seconds(secs)}，"
              f"平均速度 {human_size(avg_rate)}/s")
    else:
        print(f"[汇总] 未成功处理任何文件（跳过/失败 {failed} 个），用时 {human_seconds(secs)}", file=sys.stderr)


if __name__ == "__main__":
    main()
