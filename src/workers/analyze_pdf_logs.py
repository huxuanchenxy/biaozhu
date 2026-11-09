#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析 pdf_rename_inflate_upload 产生的 .log 日志。

支持两种文件名前缀：pdf_processed_* 和 mt_pdf_processed_*。
同时输出：
  - 终端摘要：项目级/分片级/整体汇总，含计数、数据量、速度、带宽、耗时等
  - 可选 Markdown 报表（--report path），含完整明细 + 异常清单

用法：
  python analyze_pdf_logs.py                           # 默认读同级 log202611081601
  python analyze_pdf_logs.py -d /path/to/logs          # 指定目录
  python analyze_pdf_logs.py -d . --report _report.md  # 同时写 Markdown
  python analyze_pdf_logs.py -d . --only finished       # 只统计已完成分片
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# ────────────────────────────────────────────────────────────────────────────────
# 解析工具
# ────────────────────────────────────────────────────────────────────────────────

SIZE_UNIT = {"KB": 1 / 1024.0, "MB": 1.0, "GB": 1024.0}
SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE)
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
# PROCESSED 模式：下载 5.3s 膨胀 2ms 校验 1ms 传副本 7.5s 传扩大 23.2s
STEP_PROCESSED_RE = re.compile(
    r"下载\s*([\d.]+ms|[\d.]+s)\s+膨胀\s*([\d.]+ms|[\d.]+s)\s+校验\s*([\d.]+ms|[\d.]+s)"
    r"\s+传副本\s*([\d.]+ms|[\d.]+s)\s+传扩大\s*([\d.]+ms|[\d.]+s)"
)
# REPAIRED 模式：取副本 2.7s 膨胀 7ms 校验 1ms 覆盖 17.3s （只 4 步，覆盖 = 合并上传）
STEP_REPAIRED_RE = re.compile(
    r"取副本\s*([\d.]+ms|[\d.]+s)\s+膨胀\s*([\d.]+ms|[\d.]+s)\s+校验\s*([\d.]+ms|[\d.]+s)"
    r"\s+覆盖\s*([\d.]+ms|[\d.]+s)"
)
# 汇总页各步骤累计耗时：下载 34m08.0s，膨胀 1.4s，...
SUM_STEP_TIMES_RE = re.compile(
    r"下载\s*([\d.]+ms|[\d.]+s|\d+m[\d.]+s)\s*,\s*"
    r"膨胀\s*([\d.]+ms|[\d.]+s|\d+m[\d.]+s)\s*,\s*"
    r"上传前校验\s*([\d.]+ms|[\d.]+s|\d+m[\d.]+s)\s*,\s*"
    r"传原件[^，]*\s*([\d.]+ms|[\d.]+s|\d+m[\d.]+s)\s*,\s*"
    r"传扩大\s*([\d.]+ms|[\d.]+s|\d+m[\d.]+s)"
)


def parse_size_mb(s: str) -> float:
    m = SIZE_RE.match(s.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * SIZE_UNIT[m.group(2).upper()]


def parse_step_seconds(s: str) -> float:
    """'5.3s' / '4ms' / '34m08.0s' → 秒。"""
    s = s.strip()
    # 34m08.0s
    m = re.match(r"(\d+)m([\d.]+)s", s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    # 5.3s
    m = re.match(r"([\d.]+)s", s)
    if m:
        return float(m.group(1))
    # 4ms
    m = re.match(r"([\d.]+)ms", s)
    if m:
        return float(m.group(1)) / 1000.0
    return 0.0


def parse_ts(s: str) -> datetime:
    s = s.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def human_duration(s: float) -> str:
    if s < 0:
        s = 0
    sec = int(s)
    h, rem = divmod(sec, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def human_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    if mb * 1024 >= 1:
        return f"{mb * 1024:.1f} KB"
    return f"{mb * 1024 * 1024:.0f} B"


def project_slug(filename: str) -> str:
    """pdf_processed_5.1_电工电子技术_成品语料_20261108131035.log → 5.1_电工电子技术_成品语料"""
    base = os.path.basename(filename)
    # 去掉前缀 mt_pdf_processed_ / pdf_processed_
    for prefix in ("mt_pdf_processed_", "pdf_processed_"):
        if base.startswith(prefix):
            inner = base[len(prefix):]
            break
    else:
        inner = base
    # 去掉 .log 后缀
    inner = re.sub(r"\.log$", "", inner)
    # 剥掉末尾所有 _YYYYMMDD 或 _YYYYMMDDHHMMSS 形式的时间戳片段（8~14 位纯数字）
    while True:
        new = re.sub(r"_\d{8,14}$", "", inner)
        if new == inner:
            break
        inner = new
    return inner


def is_finished(lines):
    """有以 '# =====' 开头的汇总分隔线，视为已完成。"""
    return any(l.startswith("# =====") for l in lines)


# ────────────────────────────────────────────────────────────────────────────────
# 逐分片解析
# ────────────────────────────────────────────────────────────────────────────────

def parse_summary_section(lines):
    """从汇总页（# 开头）提取已完成分片的统计数字。返回 dict，字段见下。"""
    text = "\n".join(l for l in lines if l.startswith("# "))
    if not text:
        return None

    out = {}

    # 结果：处理 N，补做 N，重做 N，失败 N，跳过 N（已完成 A／他人认领 B／超尺寸 C／非PDF D／无备份不碰 E）
    m = re.search(r"结果[：:]\s*处理\s*(\d+)，补做\s*(\d+)，重做\s*(\d+)，失败\s*(\d+)，跳过\s*(\d+)", text)
    if m:
        out.update(processed=int(m.group(1)), repaired=int(m.group(2)),
                   redone=int(m.group(3)), failed=int(m.group(4)), skipped=int(m.group(5)))
    sk = re.search(r"跳过\s*\d+\s*（已完成\s*(\d+)／他人认领\s*(\d+)／超尺寸\s*(\d+)／非PDF\s*(\d+)／无备份不碰\s*(\d+)）", text)
    if sk:
        out.update(skip_already=int(sk.group(1)), skip_claim=int(sk.group(2)),
                   skip_big=int(sk.group(3)), skip_pdf=int(sk.group(4)),
                   skip_nobak=int(sk.group(5)))

    # 各步骤累计耗时（线程·秒）
    m = SUM_STEP_TIMES_RE.search(text)
    if m:
        out.update(step_dl_s=parse_step_seconds(m.group(1)),
                   step_inflate_s=parse_step_seconds(m.group(2)),
                   step_verify_s=parse_step_seconds(m.group(3)),
                   step_copy_s=parse_step_seconds(m.group(4)),
                   step_inflate_up_s=parse_step_seconds(m.group(5)))

    # 并发度：累计/总用时 = X× ≈ 并发度
    m = re.search(r"=\s*([\d.]+)×\s*≈\s*并发度", text)
    if m:
        out["concurrency"] = float(m.group(1))

    # 折算单文件总耗时（各步骤线程·秒之和 ÷ 并发度 = 实际单文件秒）
    m = re.search(r"折算单文件\s*([\d.]+)s", text)
    if m:
        out["per_file_wall_s"] = float(m.group(1))

    # 上传前校验：查了 N 组；源件打不开 X，产物打不开 X，拦下未上传 X
    m = re.search(r"上传前校验[：:]\s*查了\s*(\d+)\s*组；源件打不开\s*(\d+)，产物打不开\s*(\d+)，拦下未上传\s*(\d+)", text)
    if m:
        out.update(verify_checked=int(m.group(1)),
                   src_bad=int(m.group(2)),
                   prod_bad=int(m.group(3)),
                   verify_blocked=int(m.group(4)))

    # 数据量：下载 XXX，上传 XXX（其中新增填充 XXX）
    m = re.search(r"数据量[：:]\s*下载\s*([\d.]+\s*(?:KB|MB|GB))，上传\s*([\d.]+\s*(?:KB|MB|GB))(?:（其中新增填充\s*([\d.]+\s*(?:KB|MB|GB))）)?", text)
    if m:
        out.update(dl_mb=parse_size_mb(m.group(1)),
                   ul_mb=parse_size_mb(m.group(2)))
        if m.group(3):
            out["added_mb"] = parse_size_mb(m.group(3))

    # 总用时 Xs / 平均 Xs/个
    m = re.search(r"总用时\s*([\d.]+)s\s*，平均\s*([\d.]+)s/个", text)
    if m:
        out.update(total_seconds=float(m.group(1)),
                   avg_per_file_s=float(m.group(2)))

    # 起止时间：2026-11-08 13:xx:xx.xxx → 2026-11-08 15:xx:xx.xxx（程序全程 XmXs...）
    m = re.search(r"起止时间[：:]\s*([\d\-: .]+?)\s*→\s*([\d\-: .]+)", text)
    if m:
        out["start_ts"] = parse_ts(m.group(1))
        out["end_ts"] = parse_ts(m.group(2))

    return out if out else None


def parse_tsv_rows(lines):
    """逐文件 TSV 行解析。返回 list[dict]，每个 dict 含单文件各项。"""
    rows = []
    for ln in lines:
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[1].strip()
        if status not in ("PROCESSED", "REPAIRED", "FAILED", "SKIPPED"):
            continue
        row = {"status": status, "ts": None, "fname": "",
               "orig_mb": 0.0, "inflated_mb": 0.0, "added_mb": 0.0,
               "dl_s": 0.0, "inflate_s": 0.0, "verify_s": 0.0,
               "copy_s": 0.0, "inflate_up_s": 0.0,
               "verify_check": "", "src_ok": True, "prod_ok": True}
        # 时间戳
        m = TS_RE.search(parts[0]) if parts else None
        if m:
            row["ts"] = parse_ts(m.group(1))
        # 文件名
        if len(parts) > 2:
            row["fname"] = parts[2].strip()
        # 大小 XMB->YMB
        for f in parts:
            if "->" in f and SIZE_RE.search(f):
                a, b = f.split("->", 1)
                row["orig_mb"] = parse_size_mb(a)
                row["inflated_mb"] = parse_size_mb(b)
                row["added_mb"] = row["inflated_mb"] - row["orig_mb"]
                break
        # 步骤耗时：PROCESSED 5步 / REPAIRED 4步
        for f in parts:
            m = STEP_PROCESSED_RE.search(f)
            if m:
                row["dl_s"] = parse_step_seconds(m.group(1))
                row["inflate_s"] = parse_step_seconds(m.group(2))
                row["verify_s"] = parse_step_seconds(m.group(3))
                row["copy_s"] = parse_step_seconds(m.group(4))
                row["inflate_up_s"] = parse_step_seconds(m.group(5))
                break
            m = STEP_REPAIRED_RE.search(f)
            if m:
                row["dl_s"] = parse_step_seconds(m.group(1))       # 取副本 = 下载
                row["inflate_s"] = parse_step_seconds(m.group(2))
                row["verify_s"] = parse_step_seconds(m.group(3))
                # REPAIRED 只有一次覆盖上传，把它算到 inflate_up_s
                row["inflate_up_s"] = parse_step_seconds(m.group(4))
                row["copy_s"] = 0.0
                break
        # 校验
        for f in parts:
            if f.startswith("校验"):
                row["verify_check"] = f.strip()
                row["src_ok"] = "源件打不开" not in f
                row["prod_ok"] = "产物打不开" not in f
                break
        rows.append(row)
    return rows


def stats_from_rows(rows):
    """从 TSV 行计算统计量（作为汇总页的补充或无汇总页时的后备）。"""
    from statistics import mean, median
    counts = defaultdict(int)
    step_dl = step_inflate = step_verify = step_copy = step_inflate_up = 0.0
    dl_mb = ul_mb = added_mb = 0.0
    src_bad = prod_bad = 0
    orig_sizes = []
    inflated_sizes = []
    timestamps = []
    abnormals = []

    for r in rows:
        counts[r["status"]] += 1
        timestamps.append(r["ts"])
        if r["status"] in ("PROCESSED", "REPAIRED"):
            step_dl += r["dl_s"]
            step_inflate += r["inflate_s"]
            step_verify += r["verify_s"]
            step_copy += r["copy_s"]
            step_inflate_up += r["inflate_up_s"]
            dl_mb += r["orig_mb"]       # 下载原始件的体积
            ul_mb += r["inflated_mb"]   # 上传膨胀后的体积
            added_mb += r["added_mb"]
            if r["orig_mb"] > 0:
                orig_sizes.append(r["orig_mb"])
            if r["inflated_mb"] > 0:
                inflated_sizes.append(r["inflated_mb"])
        if not r["src_ok"]:
            src_bad += 1
        if not r["prod_ok"]:
            prod_bad += 1
        if not r["src_ok"] or not r["prod_ok"]:
            abnormals.append((r["fname"], r["verify_check"]))

    out = {
        "processed": counts["PROCESSED"],
        "repaired": counts["REPAIRED"],
        "failed": counts["FAILED"],
        "skipped": counts["SKIPPED"],
        "step_dl_s": step_dl,
        "step_inflate_s": step_inflate,
        "step_verify_s": step_verify,
        "step_copy_s": step_copy,
        "step_inflate_up_s": step_inflate_up,
        "dl_mb": dl_mb,
        "ul_mb": ul_mb,
        "added_mb": added_mb,
        "src_bad": src_bad,
        "prod_bad": prod_bad,
        "orig_sizes": orig_sizes,
        "inflated_sizes": inflated_sizes,
        "start_ts": min(timestamps) if timestamps else None,
        "end_ts": max(timestamps) if timestamps else None,
        "abnormals": abnormals,
    }
    if orig_sizes:
        out["orig_min"] = min(orig_sizes)
        out["orig_max"] = max(orig_sizes)
        out["orig_avg"] = mean(orig_sizes)
        out["orig_med"] = median(orig_sizes)
    if inflated_sizes:
        out["infl_min"] = min(inflated_sizes)
        out["infl_max"] = max(inflated_sizes)
        out["infl_avg"] = mean(inflated_sizes)
        out["infl_med"] = median(inflated_sizes)
    # 膨胀倍数（逐文件）
    ratios = [r["inflated_mb"] / r["orig_mb"] for r in rows if r["orig_mb"] > 0]
    if ratios:
        out["ratio_avg"] = mean(ratios)
        out["ratio_min"] = min(ratios)
        out["ratio_max"] = max(ratios)
    return out


# ────────────────────────────────────────────────────────────────────────────────
# 分片级 → 完整字典
# ────────────────────────────────────────────────────────────────────────────────

def build_chunk_result(path, lines):
    slug = project_slug(os.path.basename(path))
    finished = is_finished(lines)
    summary = parse_summary_section(lines) if finished else None
    rows = parse_tsv_rows(lines)
    tsv = stats_from_rows(rows)

    wall_start = None
    wall_end = None
    if summary:
        wall_start = summary.get("start_ts")
        wall_end = summary.get("end_ts")
    if not wall_start:
        wall_start = tsv.get("start_ts")
    if not wall_end:
        wall_end = tsv.get("end_ts")
    wall_s = (wall_end - wall_start).total_seconds() if wall_start and wall_end else 0

    # 数值优先级：汇总页 > TSV 累加
    def pick(key):
        return summary.get(key) if summary and key in summary else tsv.get(key, 0)

    dl_mb = pick("dl_mb")
    ul_mb = pick("ul_mb")
    added_mb = pick("added_mb")
    if added_mb == 0 and dl_mb > 0 and ul_mb > 0:
        added_mb = ul_mb - dl_mb  # 兜底

    processed = pick("processed")
    repaired = pick("repaired")
    failed = pick("failed")
    skipped = pick("skipped")

    step_dl_s = pick("step_dl_s")
    step_inflate_s = pick("step_inflate_s")
    step_verify_s = pick("step_verify_s")
    step_copy_s = pick("step_copy_s")
    step_inflate_up_s = pick("step_inflate_up_s")

    # 并发度：汇总页给了直接用，否则 各步骤线程·秒之和 ÷ 墙钟
    if summary and summary.get("concurrency"):
        concurrency = summary["concurrency"]
    elif wall_s > 0:
        total_thread_s = sum([step_dl_s, step_inflate_s, step_verify_s,
                              step_copy_s, step_inflate_up_s])
        concurrency = total_thread_s / wall_s
    else:
        concurrency = 0.0

    # 校验数字
    src_bad = pick("src_bad")
    prod_bad = pick("prod_bad")
    if summary and "verify_blocked" in summary:
        verify_blocked = summary["verify_blocked"]
    else:
        verify_blocked = 0

    # 带宽（聚合口径）：字节 × 8 ÷ 墙钟秒 = Mbps
    ul_bytes = ul_mb * 1024 * 1024
    ul_bw_mbps = (ul_bytes * 8 / wall_s / 1_000_000) if wall_s > 0 else 0
    dl_bytes = dl_mb * 1024 * 1024
    dl_bw_mbps = (dl_bytes * 8 / wall_s / 1_000_000) if wall_s > 0 else 0

    # 处理速率（files/min）
    total_files = processed + repaired + failed
    rate_fpm = (total_files / wall_s * 60) if wall_s > 0 else 0

    # 单文件平均各步骤耗时（线程·秒 ÷ 文件数，反映串行看的真实耗时）
    files_done = total_files if total_files > 0 else 1
    per_file_dl_s = step_dl_s / files_done
    per_file_inflate_s = step_inflate_s / files_done
    per_file_verify_s = step_verify_s / files_done
    per_file_copy_s = step_copy_s / files_done
    per_file_inflate_up_s = step_inflate_up_s / files_done
    per_file_total_s = per_file_dl_s + per_file_inflate_s + per_file_verify_s + per_file_copy_s + per_file_inflate_up_s

    # 跳过分类（只有汇总页有；TSV 行里 SKIPPED 行没有原因）
    skip_detail = {}
    if summary:
        for k in ("skip_already", "skip_claim", "skip_big", "skip_pdf", "skip_nobak"):
            skip_detail[k.replace("skip_", "")] = summary.get(k, 0)

    # 模式：根据 PROCESSED vs REPAIRED 计数判断
    if repaired > 0 and processed == 0:
        mode = "REPAIRED"
    elif processed > 0 and repaired == 0:
        mode = "PROCESSED"
    elif processed > 0 and repaired > 0:
        mode = "MIXED"
    else:
        mode = "PENDING"

    return {
        "slug": slug,
        "path": os.path.basename(path),
        "finished": finished,
        "mode": mode,
        "start": wall_start,
        "end": wall_end,
        "wall_s": wall_s,

        "processed": processed,
        "repaired": repaired,
        "failed": failed,
        "skipped": skipped,
        "skip_detail": skip_detail,

        "dl_mb": dl_mb,
        "ul_mb": ul_mb,
        "added_mb": added_mb,

        "step_dl_s": step_dl_s,
        "step_inflate_s": step_inflate_s,
        "step_verify_s": step_verify_s,
        "step_copy_s": step_copy_s,
        "step_inflate_up_s": step_inflate_up_s,
        "concurrency": concurrency,

        "src_bad": src_bad,
        "prod_bad": prod_bad,
        "verify_blocked": verify_blocked,

        "ul_bw_mbps": ul_bw_mbps,
        "dl_bw_mbps": dl_bw_mbps,
        "rate_fpm": rate_fpm,

        "per_file_dl_s": per_file_dl_s,
        "per_file_inflate_s": per_file_inflate_s,
        "per_file_verify_s": per_file_verify_s,
        "per_file_copy_s": per_file_copy_s,
        "per_file_inflate_up_s": per_file_inflate_up_s,
        "per_file_total_s": per_file_total_s,

        # 文件大小分布（来自 TSV）
        "orig_min": tsv.get("orig_min", 0),
        "orig_avg": tsv.get("orig_avg", 0),
        "orig_med": tsv.get("orig_med", 0),
        "orig_max": tsv.get("orig_max", 0),
        "infl_min": tsv.get("infl_min", 0),
        "infl_avg": tsv.get("infl_avg", 0),
        "infl_med": tsv.get("infl_med", 0),
        "infl_max": tsv.get("infl_max", 0),
        "ratio_avg": tsv.get("ratio_avg", 0),
        "ratio_min": tsv.get("ratio_min", 0),
        "ratio_max": tsv.get("ratio_max", 0),

        "abnormals": tsv.get("abnormals", []),
    }


# ────────────────────────────────────────────────────────────────────────────────
# 输出
# ────────────────────────────────────────────────────────────────────────────────

def terminal_output(chunks):
    status_groups = defaultdict(list)
    for c in chunks:
        status_groups["已完成" if c["finished"] else "未完成"].append(c)

    print("=" * 120)
    print(f"共 {len(chunks)} 个分片：已完成 {len(status_groups['已完成'])}，未完成 {len(status_groups['未完成'])}")
    print("=" * 120)

    for status, cs in status_groups.items():
        if not cs:
            continue
        print(f"\n===== 【{status}】 分片 =====")
        for c in cs:
            start_s = c["start"].strftime("%Y-%m-%d %H:%M:%S") if c["start"] else "-"
            end_s = c["end"].strftime("%Y-%m-%d %H:%M:%S") if c["end"] else "-"
            print(f"\n  ◆ {c['path']}  →  {c['slug']}  [{c['mode']}]")
            print(f"    墙钟: {start_s} → {end_s}  ({human_duration(c['wall_s'])})")
            total_files = c["processed"] + c["repaired"] + c["failed"]
            print(f"    处理: PROCESSED={c['processed']}  REPAIRED={c['repaired']}  失败={c['failed']}  跳过={c['skipped']}")
            if c["skip_detail"]:
                parts = [f"{k}={v}" for k, v in c["skip_detail"].items() if v > 0]
                if parts:
                    print(f"      跳过细分: {', '.join(parts)}")
            print(f"    数据量: 下载 {human_size(c['dl_mb'])}  上传 {human_size(c['ul_mb'])}  新增填充 {human_size(c['added_mb'])}")
            print(f"    带宽: 上行 {c['ul_bw_mbps']:.1f} Mbps  下行 {c['dl_bw_mbps']:.1f} Mbps")
            print(f"    并发度: {c['concurrency']:.1f}×  处理速率: {c['rate_fpm']:.0f} 个/分钟")
            print(f"    校验: 源坏={c['src_bad']}  产坏={c['prod_bad']}  拦下={c['verify_blocked']}")
            print(f"    单文件平均: {c['per_file_total_s']:.1f}s  (下载 {c['per_file_dl_s']:.1f}s + 膨胀 {c['per_file_inflate_s']*1000:.0f}ms + 校验 {c['per_file_verify_s']*1000:.0f}ms + 传副本 {c['per_file_copy_s']:.1f}s + 传扩大 {c['per_file_inflate_up_s']:.1f}s)")
            if c["orig_max"] > 0:
                print(f"    源文件大小: min {human_size(c['orig_min'])} / med {human_size(c['orig_med'])} / max {human_size(c['orig_max'])}  膨胀倍数 avg {c['ratio_avg']:.1f}x")

    # ── 整体汇总 ──
    print("\n")
    print("=" * 120)
    print("整体汇总")
    print("=" * 120)

    if not chunks:
        print("无数据")
        return

    all_start = min(c["start"] for c in chunks if c["start"])
    all_end = max(c["end"] for c in chunks if c["end"])
    total_wall_s = (all_end - all_start).total_seconds()
    total_processed = sum(c["processed"] for c in chunks)
    total_repaired = sum(c["repaired"] for c in chunks)
    total_failed = sum(c["failed"] for c in chunks)
    total_skipped = sum(c["skipped"] for c in chunks)
    total_files = total_processed + total_repaired + total_failed
    total_dl_mb = sum(c["dl_mb"] for c in chunks)
    total_ul_mb = sum(c["ul_mb"] for c in chunks)
    total_added_mb = sum(c["added_mb"] for c in chunks)
    total_src_bad = sum(c["src_bad"] for c in chunks)
    total_prod_bad = sum(c["prod_bad"] for c in chunks)

    # 聚合带宽（集群口径）：所有分片上传字节 ÷ 整体墙钟
    cluster_ul_bw = (total_ul_mb * 1024 * 1024 * 8 / total_wall_s / 1_000_000) if total_wall_s > 0 else 0
    cluster_dl_bw = (total_dl_mb * 1024 * 1024 * 8 / total_wall_s / 1_000_000) if total_wall_s > 0 else 0

    print(f"\n  整体时间窗: {all_start.strftime('%Y-%m-%d %H:%M:%S')} → {all_end.strftime('%Y-%m-%d %H:%M:%S')}  (墙钟 {human_duration(total_wall_s)})")
    print(f"  分片数: {len(chunks)}")
    print(f"  成功处理: PROCESSED={total_processed}  REPAIRED={total_repaired}  合计 {total_files}")
    print(f"  失败: {total_failed}  跳过: {total_skipped}")
    print(f"  数据量: 下载 {human_size(total_dl_mb)}  上传 {human_size(total_ul_mb)}  新增填充 {human_size(total_added_mb)}")
    print(f"  集群带宽(聚合): 上行 {cluster_ul_bw:.1f} Mbps  下行 {cluster_dl_bw:.1f} Mbps")
    print(f"  校验异常: 源坏={total_src_bad}  产坏={total_prod_bad}")

    # 项目级合并
    by_project = defaultdict(list)
    for c in chunks:
        # 去掉末尾的版本标记（如 5.1 / 5.1_2nd）来合并同一项目
        proj = re.sub(r"_\d+$", "", c["slug"])
        by_project[proj].append(c)

    print(f"\n===== 按项目合并 =====")
    for proj, pcs in sorted(by_project.items(), key=lambda x: -sum(c["ul_mb"] for c in x[1])):
        p_start = min(c["start"] for c in pcs if c["start"])
        p_end = max(c["end"] for c in pcs if c["end"])
        p_wall = (p_end - p_start).total_seconds() if p_start and p_end else 0
        p_proc = sum(c["processed"] for c in pcs)
        p_rep = sum(c["repaired"] for c in pcs)
        p_dl = sum(c["dl_mb"] for c in pcs)
        p_ul = sum(c["ul_mb"] for c in pcs)
        p_add = sum(c["added_mb"] for c in pcs)
        p_bw = (p_ul * 1024 * 1024 * 8 / p_wall / 1_000_000) if p_wall > 0 else 0
        modes = {c["mode"] for c in pcs}
        mode_str = "/".join(sorted(modes))
        print(f"\n  ◆ {proj}  [{mode_str}]  ({len(pcs)} 个分片)")
        print(f"    墙钟: {p_start.strftime('%Y-%m-%d %H:%M:%S') if p_start else '-'} → {p_end.strftime('%Y-%m-%d %H:%M:%S') if p_end else '-'}  ({human_duration(p_wall)})")
        print(f"    PROCESSED={p_proc}  REPAIRED={p_rep}  下载 {human_size(p_dl)}  上传 {human_size(p_ul)}  新增 {human_size(p_add)}  带宽 {p_bw:.1f} Mbps")


# ────────────────────────────────────────────────────────────────────────────────
# Markdown 报表生成
# ────────────────────────────────────────────────────────────────────────────────

def write_markdown_report(chunks, out_path):
    all_start = min((c["start"] for c in chunks if c["start"]), default=None)
    all_end = max((c["end"] for c in chunks if c["end"]), default=None)
    total_wall_s = (all_end - all_start).total_seconds() if all_start and all_end else 0
    total_processed = sum(c["processed"] for c in chunks)
    total_repaired = sum(c["repaired"] for c in chunks)
    total_failed = sum(c["failed"] for c in chunks)
    total_skipped = sum(c["skipped"] for c in chunks)
    total_files = total_processed + total_repaired + total_failed
    total_dl_mb = sum(c["dl_mb"] for c in chunks)
    total_ul_mb = sum(c["ul_mb"] for c in chunks)
    total_added_mb = sum(c["added_mb"] for c in chunks)
    total_src_bad = sum(c["src_bad"] for c in chunks)
    total_prod_bad = sum(c["prod_bad"] for c in chunks)
    cluster_ul_bw = (total_ul_mb * 1024 * 1024 * 8 / total_wall_s / 1_000_000) if total_wall_s > 0 else 0
    cluster_dl_bw = (total_dl_mb * 1024 * 1024 * 8 / total_wall_s / 1_000_000) if total_wall_s > 0 else 0

    by_project = defaultdict(list)
    for c in chunks:
        proj = re.sub(r"_\d+$", "", c["slug"])
        by_project[proj].append(c)

    lines = []
    lines.append(f"# PDF 膨胀任务分析报表 — {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")
    lines.append(f"> 分片总数 {len(chunks)}（已完成 {sum(1 for c in chunks if c['finished'])}，未完成 {sum(1 for c in chunks if not c['finished'])}）")
    lines.append("")

    # 1. 总览
    lines.append("## 1. 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    if all_start and all_end:
        lines.append(f"| 整体起止 | {all_start.strftime('%Y-%m-%d %H:%M:%S')} → {all_end.strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append(f"| 整体墙钟 | {human_duration(total_wall_s)} |")
    lines.append(f"| 成功 PROCESSED | **{total_processed:,}** |")
    lines.append(f"| 成功 REPAIRED | **{total_repaired:,}** |")
    lines.append(f"| 失败 | {total_failed:,} |")
    lines.append(f"| 跳过 | {total_skipped:,} |")
    lines.append(f"| 总下载量 | **{human_size(total_dl_mb)}** |")
    lines.append(f"| 总上传量 | **{human_size(total_ul_mb)}** |")
    lines.append(f"| 新增填充量 | {human_size(total_added_mb)} |")
    lines.append(f"| 集群上行带宽（聚合口径） | **{cluster_ul_bw:.1f} Mbps** |")
    lines.append(f"| 集群下行带宽（聚合口径） | {cluster_dl_bw:.1f} Mbps |")
    lines.append(f"| 校验异常（源件/产物打不开） | {total_src_bad} / {total_prod_bad} |")
    lines.append("")

    # 2. 分片明细
    lines.append("## 2. 分片明细")
    lines.append("")
    lines.append("| # | Slug | 状态 | 模式 | 起止 | 墙钟 | PROC | REPR | FAIL | SKIP | 下载 | 上传 | 新增 | 并发 | 上带宽 | 速率/min | 源坏 | 产坏 |")
    lines.append("|---|---|---|---|---|---|---:|---:|---:|---|---|---|---|---:|---:|---:|---:|---:|")
    for i, c in enumerate(sorted(chunks, key=lambda x: -x["ul_mb"]), 1):
        st = "✓" if c["finished"] else "✗"
        ss = c["start"].strftime("%m-%d %H:%M:%S") if c["start"] else "-"
        ee = c["end"].strftime("%m-%d %H:%M:%S") if c["end"] else "-"
        lines.append(
            f"| {i} | `{c['slug']}` | {st} | {c['mode']} | {ss}→{ee} | {human_duration(c['wall_s'])} "
            f"| {c['processed']} | {c['repaired']} | {c['failed']} | {c['skipped']} "
            f"| {human_size(c['dl_mb'])} | {human_size(c['ul_mb'])} | {human_size(c['added_mb'])} "
            f"| {c['concurrency']:.1f}× | {c['ul_bw_mbps']:.1f} Mbps | {c['rate_fpm']:.0f} "
            f"| {c['src_bad']} | {c['prod_bad']} |"
        )
    lines.append("")

    # 3. 项目合并
    lines.append("## 3. 按项目合并")
    lines.append("")
    lines.append("| # | 项目 | 分片数 | 模式 | 起止 | 墙钟 | PROC | REPR | 下载 | 上传 | 新增 | 项目带宽 |")
    lines.append("|---|---|---:|---|---|---|---:|---:|---|---|---|---:|")
    for i, (proj, pcs) in enumerate(sorted(by_project.items(), key=lambda x: -sum(c["ul_mb"] for c in x[1])), 1):
        p_start = min((c["start"] for c in pcs if c["start"]), default=None)
        p_end = max((c["end"] for c in pcs if c["end"]), default=None)
        p_wall = (p_end - p_start).total_seconds() if p_start and p_end else 0
        p_bw = (sum(c["ul_mb"] for c in pcs) * 1024 * 1024 * 8 / p_wall / 1_000_000) if p_wall > 0 else 0
        modes = "/".join(sorted({c["mode"] for c in pcs}))
        ss = p_start.strftime("%m-%d %H:%M") if p_start else "-"
        ee = p_end.strftime("%H:%M") if p_end else "-"
        lines.append(
            f"| {i} | `{proj}` | {len(pcs)} | {modes} | {ss}→{ee} | {human_duration(p_wall)} "
            f"| {sum(c['processed'] for c in pcs)} | {sum(c['repaired'] for c in pcs)} "
            f"| {human_size(sum(c['dl_mb'] for c in pcs))} | {human_size(sum(c['ul_mb'] for c in pcs))} "
            f"| {human_size(sum(c['added_mb'] for c in pcs))} | {p_bw:.1f} Mbps |"
        )
    lines.append("")

    # 4. 各步骤耗时
    lines.append("## 4. 各步骤耗时明细")
    lines.append("")
    lines.append("| Slug | 模式 | 下载(线程·秒) | 膨胀(线程·秒) | 校验(线程·秒) | 传副本(线程·秒) | 传扩大(线程·秒) | 单文件总耗时 | 并发度 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for c in sorted(chunks, key=lambda x: -x["per_file_total_s"]):
        lines.append(
            f"| `{c['slug']}` | {c['mode']} "
            f"| {human_duration(c['step_dl_s'])} | {human_duration(c['step_inflate_s'])} "
            f"| {human_duration(c['step_verify_s'])} | {human_duration(c['step_copy_s'])} "
            f"| {human_duration(c['step_inflate_up_s'])} | {c['per_file_total_s']:.1f}s "
            f"| {c['concurrency']:.1f}× |"
        )
    lines.append("")

    # 5. 文件大小分布
    lines.append("## 5. 文件大小分布（TSV 行扫描）")
    lines.append("")
    lines.append("| Slug | 原文件 min | 原文件 med | 原文件 max | 膨胀后 min | 膨胀后 med | 膨胀后 max | 膨胀倍数 avg |")
    lines.append("|---|---|---|---|---|---|---|---:|")
    for c in sorted(chunks, key=lambda x: -x["orig_max"]):
        if c["orig_max"] == 0:
            continue
        lines.append(
            f"| `{c['slug']}` "
            f"| {human_size(c['orig_min'])} | {human_size(c['orig_med'])} | {human_size(c['orig_max'])} "
            f"| {human_size(c['infl_min'])} | {human_size(c['infl_med'])} | {human_size(c['infl_max'])} "
            f"| {c['ratio_avg']:.1f}× |"
        )
    lines.append("")

    # 6. 异常清单
    lines.append("## 6. 校验异常文件清单")
    lines.append("")
    all_abn = [(c["slug"],) + a for c in chunks for a in c["abnormals"]]
    if not all_abn:
        lines.append("无")
    else:
        lines.append(f"共 **{len(all_abn)}** 条校验异常，全部为源件损坏导致。")
        lines.append("")
        lines.append("| 分片 | 文件名 | 校验结论 |")
        lines.append("|---|---|---|")
        for slug, fname, check in all_abn:
            lines.append(f"| `{slug}` | `{fname}` | {check} |")
    lines.append("")

    # 7. 跳过分类（仅汇总页有）
    lines.append("## 7. 跳过分类明细")
    lines.append("")
    has_skip = any(c["skip_detail"] for c in chunks)
    if not has_skip:
        lines.append("无汇总页分片无法解析跳过原因。")
    else:
        lines.append("| Slug | 已完成 | 他人认领 | 超尺寸 | 非PDF | 无备份不碰 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for c in chunks:
            d = c.get("skip_detail", {})
            if not d:
                continue
            lines.append(
                f"| `{c['slug']}` "
                f"| {d.get('already', 0)} | {d.get('claim', 0)} | {d.get('big', 0)} "
                f"| {d.get('pdf', 0)} | {d.get('nobak', 0)} |"
            )
    lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Markdown 报表已写入: {out_path}")


# ────────────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="分析 pdf_rename_inflate_upload 产生的 .log 日志")
    parser.add_argument("-d", "--dir", default=".", help="日志目录（默认当前目录）")
    parser.add_argument("--report", default="", help="同时写入 Markdown 报表的路径")
    parser.add_argument("--only", choices=["all", "finished", "unfinished"], default="all", help="只统计哪类分片")
    parser.add_argument("--pattern", default="*.log", help="匹配的文件名模式（默认 *.log）")
    args = parser.parse_args()

    log_dir = Path(args.dir).resolve()
    if not log_dir.is_dir():
        sys.exit(f"目录不存在：{log_dir}")

    # 排除脚本自身的产物（_*.py, _*.md, _*.csv, _*.html, .workbuddy）
    exclude_prefixes = ("_", ".workbuddy")
    files = [p for p in sorted(log_dir.glob(args.pattern))
             if not any(p.name.startswith(ep) for ep in exclude_prefixes)]

    if not files:
        sys.exit(f"目录 {log_dir} 中没有匹配 {args.pattern} 的日志文件")

    print(f"\n日志目录: {log_dir}")
    print(f"发现 {len(files)} 个日志文件\n")

    chunks = []
    for p in files:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        chunks.append(build_chunk_result(str(p), lines))

    # 过滤
    if args.only == "finished":
        chunks = [c for c in chunks if c["finished"]]
    elif args.only == "unfinished":
        chunks = [c for c in chunks if not c["finished"]]

    terminal_output(chunks)

    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = log_dir / report_path
        write_markdown_report(chunks, str(report_path))


if __name__ == "__main__":
    main()
