#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览 .out 日志文件末尾内容（类似对每个文件执行 tail -n 10）。

用法：
  python preview_out_logs.py                  # 预览脚本同级的 log202611090828 目录
  python preview_out_logs.py -n 20            # 每个日志显示最后 20 行
  python preview_out_logs.py D:\\logs         # 指定其它日志目录
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

# 日志末尾出现这些字样，视为该任务已正常收尾
FINISH_MARKERS = ("起止时间：", "===== 统计", "总用时", "扫描结束：")
# 末尾出现这些字样，视为异常退出
ERROR_MARKERS = ("Traceback (most recent call last)", "KeyboardInterrupt")

# 进度行里形如 [12m34.5s] 或 [2584/2584] 的行，用来提取"最新进度"
PROGRESS_RE = re.compile(r"\[(?:\d+m)?\d+(?:\.\d+)?s\]|\[\d+/\d+\]")

DEFAULT_LINES = 10
DEFAULT_DIR = Path(__file__).resolve().parent / "log202611090828"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def read_lines(path: Path):
    """读取整个日志并返回所有行（日志一般 < 1MB，直接读整文件最省事）。"""
    text = path.read_bytes().decode("utf-8", errors="replace")
    return text.splitlines()


def judge_status(lines, mtime, running_minutes):
    """根据日志末尾内容和文件修改时间判断任务状态。

    说明：本地拿到的可能只是远端日志的拷贝，修改时间不一定等于进程存活状态，
    因此"未收尾"只表示日志里没有正常结束的统计段。
    """
    tail = lines[-40:]
    if any(marker in line for line in tail for marker in ERROR_MARKERS):
        return "异常退出"
    if any(marker in line for line in tail for marker in FINISH_MARKERS):
        return "已完成"

    age_minutes = (datetime.now().timestamp() - mtime) / 60
    if age_minutes <= running_minutes:
        return "运行中"
    return f"未收尾（{int(age_minutes)} 分钟前最后输出）"


def latest_progress(lines):
    """取最后一条带进度标记的行，作为该任务最新进度摘要。"""
    for line in reversed(lines):
        if PROGRESS_RE.search(line):
            return line.strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description="按 tail 方式预览每个 .out 日志的最后若干行")
    parser.add_argument("log_dir", nargs="?", default=str(DEFAULT_DIR), help="日志目录（默认脚本同级的 log202611090828）")
    parser.add_argument("-n", "--lines", type=int, default=DEFAULT_LINES, help=f"每个日志显示的行数（默认 {DEFAULT_LINES}）")
    parser.add_argument("--running-within", type=int, default=10, help="最近多少分钟内仍有输出视为运行中（默认 10 分钟）")
    parser.add_argument("--pattern", default="*.out", help="匹配的文件名模式（默认 *.out）")
    args = parser.parse_args()

    # Windows 控制台默认 cp936，日志里的 ✓ 等字符会报编码错误
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = Path.cwd() / log_dir
    if not log_dir.is_dir():
        sys.exit(f"目录不存在：{log_dir}")

    files = sorted(log_dir.glob(args.pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        sys.exit(f"目录中没有匹配 {args.pattern} 的文件：{log_dir}")

    status_counts = {}
    for path in files:
        stat = path.stat()
        lines = read_lines(path)
        status = judge_status(lines, stat.st_mtime, args.running_within)
        category = status.split("（")[0]
        status_counts[category] = status_counts.get(category, 0) + 1

        print("=" * 100)
        print(
            f"{path.name}    {human_size(stat.st_size)}    "
            f"{datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}    "
            f"共 {len(lines)} 行    【{status}】"
        )
        progress = latest_progress(lines)
        if progress:
            print(f"最新进度：{progress}")
        print("-" * 100)
        for line in lines[-args.lines:] if args.lines > 0 else []:
            print(line)
        print()

    print("=" * 100)
    summary = "，".join(f"{k} {v} 个" for k, v in status_counts.items())
    print(f"共 {len(files)} 个日志：{summary}（按修改时间倒序）")


if __name__ == "__main__":
    main()
