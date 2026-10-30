#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真正的 PDF 内核校验 PDF 到底坏没坏（只读，不改动任何文件）。

为什么还要再做一版：pdf_check_damaged.py 是自己按字节读结构判的（零依赖、能扫几十万份），
但它不是阅读器。这一版直接请阅读器内核来判，结论贴近「用户点开能不能看」。
实测得到的分层结论（本机 bad.pdf 与一批样本）：
  - fitz(PyMuPDF)  ：唯一会明确报「找不到 startxref、走了修复分支」的内核，信号最丰富；
                     它修好后照样能渲染 5 页，所以「能打开」绝不等于「没坏」，必须看
                     is_repaired + mupdf_warnings()。前端 mupdf 视图就是这个内核。
  - pypdfium2      ：Chrome/Edge 内置 PDF 查看器的同内核，最贴近「人工双击打开」。
                     实测对旧填充产物也放行，但对截断件/空壳直接抛 Data format error。
  - pdf.js         ：前端翻译视图内核（走 node + 同目录 pdf_check_pdfjs.mjs，--pdfjs 开启）。
                     实测对旧填充产物同样放行。
  - pdfminer/plumber：纯 Python，宽容且慢（单份 1~3s），实测对 startxref 类问题一律报 ok，
                     只能当旁证，所以默认不进 auto，显式点名才用。
结论：单靠「某个内核能不能打开」判好坏是不成立的——三个内核都会各自修复后放行。真正可靠的
判据是「打开成败 + 是否走修复分支 + 修复原因指纹 + 页面有没有画出内容」四项合起来看。

判据与等级：
  OK               干净打开、无修复痕迹、抽样页有内容
  LIB_REPAIRED     引擎走了修复分支或报格式警告 → 按警告指纹分两档：
                   致命指纹（找不到 startxref/trailer、xref 偏移越界）→ 默认判「损坏」，WPS 不替你修复；
                   琐碎告警（书签树、回退字体）→ 最高只到「可疑」，绝不判损坏。
                   --repaired-as 只调致命那一档；想看浏览器视角（能修好就算过）写 --repaired-as warn
  LIB_OPEN_FAIL    引擎根本打不开 → 损坏
  LIB_RENDER_FAIL  能打开但页画不出来 → 损坏
  BLANK_PAGE       能画但整页几乎全白（内容丢了/图片没进来）→ 默认可疑，--blank-as broken 可提级
  NOT_PDF          头部没有 %PDF- 魔数            EMPTY       0 字节
  PAGES_ZERO       能打开但页数为 0
四条实测踩出来的红线，别写反：
  - 不能拿「提取不到文字」判损坏：bad.pdf 文本层 0 字符，页面画面却完全正常（扫描件/图片型）。
  - 不能拿「能渲染」判正常：bad.pdf 三个内核都渲染成功，它照样是要重做的旧填充产物。
  - 也不能拿「走了修复分支」一概判坏：15DZ2260300-01.pdf（新规则产物）只有一条
    「repaired broken tree structure in outline」书签树告警，WPS 实测能正常打开。
  - 更不能拿「xref 被重建」判坏：04A11310…/04801490…（iText 增量更新件）报
    「expected trailer marker」+「trying to repair broken xref」，但尾部 startxref 距尾只 23 字节、
    指向确实是 xref 表，17/10 页全部渲染正常——2026-10-29 这两份加一份 CLEAN 对照人肉用 WPS
    点验，三份全部正常打开。它们跟 bad.pdf（尾部根本找不到 startxref、真指针被推到距尾 9.4MB，
    WPS 报损坏）不是一回事。
    能判坏的只有「尾部指针不可用」那一类致命指纹。

用法（默认就是 WPS 那种严格口径，一条命令够了）：
  python pdf_check_lib.py bad.pdf                      # ← 就这个：一句大白话告诉你坏没坏、坏在哪
  python pdf_check_lib.py pdf/                          # 目录也行，递归
  python pdf_check_lib.py pdf/ -n 200 --procs 4 -q      # 批量摸底（多进程；fitz 警告收集器是进程级全局）
  python pdf_check_lib.py pdf/ --no-render --procs 8 -q --csv 校验.csv --list-file 坏清单.txt
  python pdf_check_lib.py bad.pdf --show-warnings       # 看引擎原始警告逐行
  python pdf_check_lib.py bad.pdf --with-struct --pdfjs # 叠内置结构判定 / 前端 pdf.js 内核对照
  python pdf_check_lib.py bad.pdf --repaired-as warn    # 换宽容口径：需修复只算可疑（浏览器视角）

依赖：至少要装一个内核，脚本会给出安装命令：
  pip install pymupdf          # fitz（MuPDF）
  pip install pypdfium2        # PDFium，浏览器原生同款
  pip install pdfminer.six pdfplumber
  --pdfjs 需要本机有 node，且项目里装了 pdfjs-dist（前端已有）
退出码：0=没有损坏；1=检出损坏；2=参数/环境错误。
"""

import argparse
import csv
import glob
import io
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

OK = "ok"
WARN = "warn"
BROKEN = "broken"
LEVEL_ORDER = {BROKEN: 0, WARN: 1, OK: 2}
LEVEL_MARK = {OK: "✓", WARN: "!", BROKEN: "✗", "skip": "-"}
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 「严格阅读器会挂」的指纹：只收尾部指针不可用那一类。判定样本是 bad.pdf 与旧规则产物
# （指针被推走 / 偏移说谎），WPS 实测报损坏。
# 已按实测踢出两条：“expected trailer”与“trying to repair broken xref”——它们是内核重建 xref
# 的流水，iText 增量更新件也报但 WPS 能正常打开，当判坏依据会大面积误杀。
FATAL_PATTERNS = ("cannot find startxref", "cannot find the trailer",
                  "cannot build xref", "xref offset invalid",
                  "cannot find pdf header", "page type instead of xref")
# 「内核重建了 xref 才读到」但不等于打不开：单独一档只记不拦
REBUILD_PATTERNS = ("expected trailer", "trying to repair broken xref", "repairing pdf document")
# 「与能不能打开无关」的指纹：书签树、字体替换一类，别当损坏
COSMETIC_PATTERNS = ("outline", "tree structure", "fallback font", "cannot find font")
FAST_AUTO = ("fitz", "pdfium")          # auto 只用这两个：快，且分别对应 mupdf 与浏览器原生
SLOW_ENGINES = ("pdfminer", "pdfplumber")


def ensure_utf8_streams():
    """控制台编不出中文/✓✗ 时降级为 replace，别在打印结论那行崩掉。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            "中文✓".encode((getattr(s, "encoding", "") or "ascii"))
            s.reconfigure(errors="replace")
        except (UnicodeEncodeError, LookupError, ValueError):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


def human_size(num) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}TB"


def human_secs(sec: float) -> str:
    sec = max(float(sec), 0.0)
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    return f"{int(m)}m{s:04.1f}s"


def read_head(path: str, n: int = 5) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def basic_gate(path: str, r: dict) -> bool:
    """0 字节 / 非 PDF 这类铁证，各家引擎不必再试；True 表示已定案。"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        r.update(code="READ_ERROR", level=BROKEN, note=f"读不到文件：{e}")
        return True
    if size == 0:
        r.update(code="EMPTY", level=BROKEN, note="0 字节")
        return True
    if not read_head(path).startswith(b"%PDF-"):
        r.update(code="NOT_PDF", level=BROKEN, note="头部没有 %PDF- 魔数")
        return True
    return False


def classify_warns(lines) -> tuple:
    """把引擎警告分成致命/需重建/琐碎三堆，返回 (fatal命中, cosmetic命中, rebuild命中)。"""
    fatal = [w for w in lines if any(k in w.lower() for k in FATAL_PATTERNS)]
    cos = [w for w in lines if any(k in w.lower() for k in COSMETIC_PATTERNS)]
    rebuild = [w for w in lines if any(k in w.lower() for k in REBUILD_PATTERNS)]
    return fatal, cos, rebuild


def grade_repaired(repaired_as: str, fatal: bool) -> str:
    """修复分支那一档的分级：只有致命指纹才吃 --repaired-as（默认 broken）。

    琐碎告警（书签树坏了一节点、缺字形回退字体）一律拉到可疑为止：实测这类 WPS 照常打开，
    跟着修复分支一起判坏会大面积误杀（15DZ2260300-01.pdf 就是这么错判的）。
    """
    if fatal:
        return repaired_as
    return WARN if repaired_as == BROKEN else repaired_as


# ---------------- 引擎实现：统一返回 dict(engine/code/level/pages/note/warns) ----------------

def eng_fitz(path: str, opt) -> dict:
    """MuPDF 内核。is_repaired + mupdf_warnings 是「宽容读也发现了坏」的唯一证据。"""
    import fitz
    r = {"engine": "fitz", "code": OK, "level": OK, "pages": None, "note": "", "warns": []}
    if basic_gate(path, r):
        return r
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    fitz.TOOLS.reset_mupdf_warnings()      # 警告收集器是进程级全局，每次开工先清空
    try:
        doc = fitz.open(path)
    except Exception as e:
        r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
        return r
    try:
        warn_text = fitz.TOOLS.mupdf_warnings() or ""
        pages = doc.page_count
        repaired = bool(getattr(doc, "is_repaired", False))
        r["pages"] = pages
        r["warns"] = [ln for ln in warn_text.splitlines() if ln.strip()][:8]
        if pages == 0:
            r.update(code="PAGES_ZERO", level=BROKEN, note="页数为 0")
            return r
        blanks, fails = [], []
        if opt.render:
            for i in range(min(pages, opt.max_pages)):
                try:
                    pm = doc[i].get_pixmap(dpi=opt.dpi)
                    if pm.width <= 0 or pm.height <= 0 or not pm.samples:
                        fails.append(i + 1)
                        continue
                    gray = pm.samples[0::(pm.n or 1)]           # 只取一个通道，C 速度算墨量
                    total = len(gray)
                    ink = (total - gray.count(b"\xff")) / total if total else 0.0
                    if ink < opt.blank_ink:
                        blanks.append((i + 1, f"{ink:.2%}"))
                except Exception as e:
                    fails.append((i + 1, str(e)[:50]))
        if fails:
            r.update(code="LIB_RENDER_FAIL", level=BROKEN,
                     note=f"抽样 {min(pages, opt.max_pages)} 页中 {len(fails)} 页画不出来：{fails[:3]}")
        elif repaired or r["warns"]:
            fatal, cos, rebuild = classify_warns(r["warns"])
            r.update(code="LIB_REPAIRED", level=grade_repaired(opt.repaired_as, bool(fatal)),
                     note=("MuPDF 走修复分支" if repaired else "MuPDF 报格式警告")
                          + ("；含致命指纹，按 xref 定位的严格读法会挂" if fatal
                             else "；内核重建了 xref（指针在位、页码齐全，实测能打开）" if rebuild
                             else "；仅琐碎告警（书签/字体一类），不该判坏" if cos
                             else "；修复原因未归类，不当损坏处理"))
        elif blanks:
            r.update(code="BLANK_PAGE", level=opt.blank_as, note=f"抽样页里 {len(blanks)} 页近乎全白：{blanks[:3]}")
        else:
            r["note"] = f"干净打开 {pages} 页" + (f"，抽样渲染 {min(pages, opt.max_pages)} 页有内容"
                                                if opt.render else "")
        return r
    finally:
        try:
            doc.close()
        except Exception:
            pass


def eng_pdfium(path: str, opt) -> dict:
    """PDFium：Chrome/Edge 内置查看器同内核，最接近「人工双击打开」的结果。"""
    import pypdfium2 as pdfium
    r = {"engine": "pdfium", "code": OK, "level": OK, "pages": None, "note": "", "warns": []}
    if basic_gate(path, r):
        return r
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as e:
        r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
        return r
    try:
        pages = len(pdf)
        r["pages"] = pages
        if pages == 0:
            r.update(code="PAGES_ZERO", level=BROKEN, note="页数为 0")
            return r
        blanks, fails = [], []
        if opt.render:
            for i in range(min(pages, opt.max_pages)):
                try:
                    arr = pdf[i].render(scale=opt.dpi / 72.0).to_numpy()
                    ink = float((arr.min(axis=2) < 250).mean()) if arr.size else 0.0
                    if ink < opt.blank_ink:
                        blanks.append((i + 1, f"{ink:.2%}"))
                except Exception as e:
                    fails.append((i + 1, str(e)[:50]))
        if fails:
            r.update(code="LIB_RENDER_FAIL", level=BROKEN,
                     note=f"抽样渲染失败 {len(fails)} 页：{fails[:3]}")
        elif blanks:
            r.update(code="BLANK_PAGE", level=opt.blank_as, note=f"抽样页里 {len(blanks)} 页近乎全白：{blanks[:3]}")
        else:
            r["note"] = f"浏览器同款内核打开成功 {pages} 页" + (f"，渲染 {min(pages, opt.max_pages)} 页有内容"
                                                              if opt.render else "")
        return r
    finally:
        try:
            pdf.close()
        except Exception:
            pass


class _GrabHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.msgs = []

    def emit(self, record):
        try:
            self.msgs.append(record.getMessage())
        except Exception:
            pass


def eng_pdfminer(path: str, opt) -> dict:
    """pdfminer.six：宽松解析器，问题多走 logging；实测对 startxref 类问题会报 ok，只当旁证。"""
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LAParams
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    r = {"engine": "pdfminer", "code": OK, "level": OK, "pages": None, "note": "", "warns": []}
    if basic_gate(path, r):
        return r
    logger = logging.getLogger("pdfminer")
    handler = _GrabHandler()
    logger.addHandler(handler)
    old_level, logger.level = logger.level, logging.WARNING
    try:
        with open(path, "rb") as f:
            try:
                PDFDocument(PDFParser(f))
            except Exception as e:
                r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
                return r
            f.seek(0)
            try:
                pages = len(list(PDFPage.get_pages(f)))
            except Exception as e:
                r.update(code="LIB_OPEN_FAIL", level=BROKEN,
                         note=f"取页面失败 {type(e).__name__}: {e}"[:220])
                return r
            r["pages"] = pages
            if opt.render and pages:
                f.seek(0)
                rsrc = PDFResourceManager()
                dev = PDFPageAggregator(rsrc, laparams=LAParams())
                interp = PDFPageInterpreter(rsrc, dev)
                bad = 0
                for i, page in enumerate(PDFPage.get_pages(f)):
                    if i >= opt.max_pages:
                        break
                    try:
                        interp.process_page(page)
                        dev.get_result()
                    except Exception:
                        bad += 1
                if bad:
                    r.update(code="LIB_RENDER_FAIL", level=BROKEN,
                             note=f"内容流解码 {bad} 页失败（抽样 {min(pages, opt.max_pages)} 页）")
                    return r
        warns = [m for m in handler.msgs if m]
        if warns:
            r["warns"] = warns[:8]
            fatal, _ = classify_warns(warns)
            r.update(code="LIB_REPAIRED", level=grade_repaired(opt.repaired_as, bool(fatal)),
                     note=f"pdfminer 报出 {len(warns)} 条警告")
        else:
            r["note"] = f"干净解析 {pages} 页"
        return r
    except Exception as e:
        r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
        return r
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def eng_pdfplumber(path: str, opt) -> dict:
    """pdfplumber：在 pdfminer 之上真取词，作为「内容层能不能用」的旁证（慢）。"""
    import warnings
    import pdfplumber

    r = {"engine": "pdfplumber", "code": OK, "level": OK, "pages": None, "note": "", "warns": []}
    if basic_gate(path, r):
        return r
    buf = io.StringIO()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                with pdfplumber.open(path) as pdf:
                    r["pages"] = len(pdf.pages)
                    for i in range(min(len(pdf.pages), opt.max_pages if opt.render else 0)):
                        pdf.pages[i].extract_words()
            except Exception as e:
                r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
                return r
            for w in caught:
                buf.write(f"{w.category.__name__}: {w.message}\n")
        text = buf.getvalue().strip()
        if text:
            r["warns"] = text.splitlines()[:8]
            fatal, _ = classify_warns(r["warns"])
            r.update(code="LIB_REPAIRED", level=grade_repaired(opt.repaired_as, bool(fatal)),
                     note="pdfplumber 有告警")
        else:
            r["note"] = f"干净读取 {r['pages']} 页"
        return r
    except Exception as e:
        r.update(code="LIB_OPEN_FAIL", level=BROKEN, note=f"{type(e).__name__}: {e}"[:220])
        return r


ENGINE_FUNCS = {"fitz": eng_fitz, "pdfium": eng_pdfium,
               "pdfminer": eng_pdfminer, "pdfplumber": eng_pdfplumber}


def struct_ref(path: str, win: int) -> dict:
    """可选对照：内置结构判定（同目录 pdf_check_damaged.py），没有那个文件就安静跳过。"""
    try:
        if _SCRIPT_DIR not in sys.path:
            sys.path.insert(0, _SCRIPT_DIR)
        import pdf_check_damaged as chk
    except ImportError:
        return {"engine": "struct", "code": "-", "level": OK, "pages": None,
                "note": "未找到 pdf_check_damaged.py，跳过对照", "warns": []}
    try:
        res = chk.inspect(path, win)
        return {"engine": "struct", "code": res.get("code", "?"), "level": res.get("level", OK),
                "pages": res.get("pages"), "note": "；".join(res.get("notes") or [])[:200], "warns": []}
    except Exception as e:
        return {"engine": "struct", "code": "CHECK_ERROR", "level": WARN, "pages": None,
                "note": f"内置校验异常 {type(e).__name__}: {e}"[:160], "warns": []}


def pick_engines(raw: str) -> list:
    """auto=装了哪个内核用哪个（只取快的两个）；all=快的+慢的旁证；也可点名逗号分隔。"""
    avail = []
    for name, mod in (("fitz", "fitz"), ("pdfium", "pypdfium2"),
                      ("pdfminer", "pdfminer"), ("pdfplumber", "pdfplumber")):
        try:
            __import__(mod)
            avail.append(name)
        except ImportError:
            pass
    if raw == "auto":
        chosen = [e for e in FAST_AUTO if e in avail]
        if not chosen:
            chosen = [e for e in SLOW_ENGINES if e in avail]
        return chosen
    if raw == "all":
        return avail
    want = [x.strip() for x in raw.split(",") if x.strip()]
    out = [e for e in want if e in avail]
    if not out:
        miss = [e for e in want if e not in avail]
        sys.exit(f"错误：指定的引擎都没装：{'、'.join(miss)}（本机可用：{'、'.join(avail) or '无'}）\n"
                 "安装：pip install pymupdf pypdfium2 pdfminer.six pdfplumber")
    return out


def check_file(path: str, engines: list, opt_d: dict) -> dict:
    """单文件全结论。在子进程里跑也行：fitz 的警告收集器是进程级全局，跨进程才互不串味。"""
    t0 = time.time()
    rows = []
    for name in engines:
        try:
            rows.append(ENGINE_FUNCS[name](path, argparse.Namespace(**opt_d)))
        except Exception as e:
            rows.append({"engine": name, "code": "CHECK_ERROR", "level": BROKEN,
                         "pages": None, "note": f"{type(e).__name__}: {e}"[:200], "warns": []})
    if opt_d.get("with_struct"):
        rows.append(struct_ref(path, opt_d.get("tail_win", 1024)))
    real = [r for r in rows if r["code"] != "-"]
    worst = min(real, key=lambda x: LEVEL_ORDER.get(x["level"], 1)) if real else None
    fatal = any(any(k in w.lower() for k in FATAL_PATTERNS) for r in real for w in (r.get("warns") or []))
    return {"path": path, "size": os.path.getsize(path),
            "level": worst["level"] if worst else OK, "code": worst["code"] if worst else OK,
            "pages": worst["pages"] if worst else None, "engines": rows,
            "agree": len({r["code"] for r in real}) <= 1, "fatal": fatal, "t": time.time() - t0}


# ---------------- pdf.js 批检（一次 node 启动跑完全部，省掉每文件重启进程） ----------------

def pdfjs_batch(files: list, timeout: int) -> dict:
    """返回 {path: engine_row}。没有 node/脚本/pdfjs-dist 就整体跳过并说明原因。"""
    def skip(reason):
        return {f: {"engine": "pdfjs", "code": "-", "level": OK, "pages": None,
                    "note": reason, "warns": []} for f in files}
    script = os.path.join(_SCRIPT_DIR, "pdf_check_pdfjs.mjs")
    if not os.path.isfile(script):
        return skip("缺少 pdf_check_pdfjs.mjs，跳过 pdf.js 内核")
    node = shutil.which("node")
    if not node:
        return skip("本机没有 node，跳过 pdf.js 内核")
    try:
        p = subprocess.run([node, script] + files, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, check=False,
                           cwd=_SCRIPT_DIR)
    except (OSError, subprocess.TimeoutExpired) as e:
        return skip(f"调用 node 失败：{type(e).__name__}: {e}")
    out = (p.stdout or b"").decode("utf-8", "replace")
    if "Cannot find package" in out or "ERR_MODULE_NOT_FOUND" in out:
        return skip("pdfjs-dist 未安装或不可用，跳过 pdf.js 内核")
    lines = out.splitlines()
    got, res = [], {}
    for i, ln in enumerate(lines):
        if not ln.startswith("RESULT\t"):
            continue
        got.append(ln)
    if len(got) != len(files):
        err = next((ln for ln in lines if not ln.startswith("RESULT\t")), "")
        return skip(f"node 侧只回 {len(got)}/{len(files)} 条，跳过"
                    + (f"：{err[:120]}" if err else ""))
    for ln, f in zip(got, files):
        _, verdict, ms, pages, detail = ln.split("\t", 4)
        if verdict == "OK":
            res[f] = {"engine": "pdfjs", "code": OK, "level": OK, "pages": int(pages or 0),
                      "note": f"前端同款内核打开成功 {pages} 页（{detail}，{int(ms)}ms）", "warns": []}
        else:
            res[f] = {"engine": "pdfjs", "code": "LIB_OPEN_FAIL", "level": BROKEN, "pages": 0,
                      "note": f"pdf.js 打不开：{detail}（{int(ms)}ms）", "warns": []}
    return res


# ---------------- 收集与输出 ----------------

def collect(paths: list, limit: int) -> list:
    out = []
    for p in paths:
        if os.path.isfile(p):
            if p.lower().endswith(".pdf"):
                out.append(os.path.abspath(p))
        elif os.path.isdir(p):
            for root, sub, files in os.walk(p):
                sub[:] = [s for s in sub if not s.startswith(".")]
                for fn in files:
                    if fn.lower().endswith(".pdf"):
                        out.append(os.path.abspath(os.path.join(root, fn)))
        else:
            hits = glob.glob(p)
            if hits:
                out.extend(os.path.abspath(h) for h in hits if h.lower().endswith(".pdf"))
            else:
                print(f"警告：路径不存在或不是 .pdf，已忽略：{p}")
    out = sorted(set(out))
    return out[:limit] if limit > 0 else out


CODE_WHY = {
    "NOT_PDF": "根本不是 PDF（开头没有 %PDF- 魔数），像份写坏的空壳",
    "EMPTY": "0 字节的空文件",
    "PAGES_ZERO": "能读到文件但一页都没有，结构已废",
    "READ_ERROR": "本地都读不到",
    "LIB_OPEN_FAIL": "阅读器直接打不开",
    "LIB_RENDER_FAIL": "能打开但页面画不出来（内容流或字体层坏了）",
    "BLANK_PAGE": "有页面几乎全白，内容可能丢了",
}


def verdict_text(row: dict) -> str:
    """一句大白话结论：坏没坏 + 为什么。不要让人去查结论码。"""
    name = os.path.basename(row["path"])
    code, level = row["code"], row["level"]
    if level == OK:
        return (f"✓ {name} 没坏：干净打开" + (f" {row['pages']} 页" if row.get("pages") else "")
                + "，没有需要修复的痕迹")
    tail = "坏了" if level == BROKEN else "可疑"
    if code == "LIB_REPAIRED":
        if row.get("fatal"):
            why = ("尾部找不到 startxref，真指针被推到文件深处——WPS 这类严格按指针定位的阅读器会报「文件损坏」，"
                   "浏览器和 pdf.js 会自己扫全文重建所以看着正常")
        else:
            why = "结构有小瑕疵（书签一类），阅读器会自行修复，一般还能打开"
    else:
        why = CODE_WHY.get(code, code)
    return f"{LEVEL_MARK.get(level, '?')} {name} {tail}：{why}"


def print_detail(row: dict, show_warns: bool):
    print(verdict_text(row) + f"   （{human_size(row['size'])}，{row['t']:.2f}s）")
    for e in row["engines"]:
        if e["code"] == "-":
            continue
        pages = f" {e['pages']}页" if e.get("pages") else ""
        print(f"      {e['engine']:10s} {LEVEL_MARK.get(e['level'], '?')} {e['code']:16s}{pages} {e['note'][:150]}")
        if show_warns:
            for w in (e.get("warns") or []):
                tag = "  ←严格阅读器会挂" if any(k in w.lower() for k in FATAL_PATTERNS) else ""
                print(f"          · {w[:180]}{tag}")


def main():
    ensure_utf8_streams()
    p = argparse.ArgumentParser(description="用 PDF 内核（MuPDF/PDFium/pdf.js/pdfminer）校验文件坏没坏（只读）",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("paths", nargs="+", help="PDF 文件、目录或通配串")
    p.add_argument("--engine", default="auto",
                   help="auto=fitz+pdfium（快、贴浏览器）；all=再加 pdfminer/pdfplumber；或点名 fitz,pdfium,...")
    p.add_argument("--pdfjs", action="store_true", help="再叠一层前端同款 pdf.js 内核（需要 node）")
    p.add_argument("-n", "--limit", type=int, default=0, help="最多校验几个（0=全部）")
    p.add_argument("--procs", type=int, default=1, help="并行进程数（各进程独立内核状态；1=串行）")
    p.add_argument("--no-render", action="store_true", help="不渲染页面，只看打开与修复信号（最快）")
    p.add_argument("--max-pages", type=int, default=5, help="每份抽样渲染/解码的页数上限")
    p.add_argument("--dpi", type=int, default=36, help="渲染 dpi（只为确认画得出内容，越小越快）")
    p.add_argument("--blank-ink", type=float, default=0.0005,
                   help="整页墨量比例低于此值算全白（实测正常图纸页 ≥3%%，白纸 ≈0）")
    p.add_argument("--repaired-as", choices=[BROKEN, WARN, OK], default=BROKEN,
                   help="引擎走修复分支/报格式警告时判成什么：默认 broken（按 WPS 这类不替你修复的阅读器为准；"
                        "想看浏览器视角用 warn）")
    p.add_argument("--blank-as", choices=[BROKEN, WARN, OK], default=WARN, help="全白页判成什么")
    p.add_argument("--with-struct", action="store_true", help="叠一栏内置结构判定（pdf_check_damaged.py）对照")
    p.add_argument("--tail-win", type=int, default=1024, help="内置结构判定的尾部回搜窗口（字节）")
    p.add_argument("--show-warnings", action="store_true", help="逐行贴引擎原始警告")
    p.add_argument("--show-all", action="store_true", help="正常文件也打印明细（默认只打非 OK）")
    p.add_argument("-q", "--quiet", action="store_true", help="只打汇总，不打逐文件")
    p.add_argument("--csv", default="", help="CSV 报告路径（空=不写）")
    p.add_argument("--list-file", default="", help="把判损坏的路径写成清单（空=不写）")
    p.add_argument("--node-timeout", type=int, default=600, help="pdf.js 批检整体超时（秒）")
    args = p.parse_args()

    engines = pick_engines(args.engine)
    if not engines and not args.pdfjs:
        print("错误：一个 PDF 内核都没装。装一个再跑：pip install pymupdf pypdfium2")
        return 2
    files = collect(args.paths, args.limit)
    if not files:
        print("错误：没有匹配到任何 .pdf（检查路径/后缀大小写）")
        return 2

    opt_d = {"render": not args.no_render, "max_pages": args.max_pages, "dpi": args.dpi,
             "repaired_as": args.repaired_as, "blank_as": args.blank_as,
             "blank_ink": args.blank_ink, "with_struct": args.with_struct,
             "tail_win": args.tail_win}
    print(f"内核：{'、'.join(engines)}" + ("、pdfjs" if args.pdfjs else "") +
          f"；口径：startxref 类致命问题算 {args.repaired_as}，琐碎告警最高 warn，全白页算 {args.blank_as}；"
          f"共 {len(files)} 个文件，{args.procs} 进程"
          + (f"（每份渲染≤{args.max_pages}页）" if not args.no_render else "（不渲染）") + "\n")

    t0 = time.time()
    results = []
    if args.procs > 1:
        # 逐条结论最后统一打：--pdfjs 是批检结束后才合进来的，早打就漏了那一列
        with ProcessPoolExecutor(max_workers=args.procs) as pool:
            for r in pool.map(check_file, files, [engines] * len(files), [opt_d] * len(files)):
                results.append(r)
                if len(files) > 20 and len(results) % 100 == 0:
                    print(f"  …已检查 {len(results)}/{len(files)}", flush=True)
    else:
        for i, f in enumerate(files, 1):
            results.append(check_file(f, engines, opt_d))
            if len(files) > 20 and i % 100 == 0:
                print(f"  …已检查 {i}/{len(files)}", flush=True)

    if args.pdfjs:
        js = pdfjs_batch(files, args.node_timeout)
        for r in results:
            row = js.get(r["path"])
            if row:
                r["engines"].append(row)
                if row["level"] == BROKEN:
                    r["level"], r["code"] = BROKEN, "LIB_OPEN_FAIL"
                elif row["level"] == WARN and r["level"] == OK:
                    r["level"], r["code"] = WARN, row["code"]
        if results and results[0]["engines"] and results[0]["engines"][-1]["code"] == "-":
            print(f"pdf.js 内核跳过：{results[0]['engines'][-1]['note']}\n")

    secs = time.time() - t0
    cnt = {OK: 0, WARN: 0, BROKEN: 0}
    for r in results:
        cnt[r["level"]] = cnt.get(r["level"], 0) + 1
    if not args.quiet:
        for r in results:
            if len(files) == 1 or r["level"] != OK or args.show_all:
                print_detail(r, args.show_warnings)
    if len(results) > 1:
        print("\n===== 校验完成 =====")
        print(f"合计 {len(results)} 个，用时 {human_secs(secs)}：正常 {cnt[OK]}，"
              f"可疑 {cnt[WARN]}，损坏 {cnt[BROKEN]}")
        by_code = {}
        for r in results:
            by_code[r["code"]] = by_code.get(r["code"], 0) + 1
        print("结论码：" + "，".join(f"{c} {n}" for c, n in sorted(by_code.items(), key=lambda x: -x[1])))
        fatal_n = sum(1 for r in results if r.get("fatal"))
        if fatal_n:
            print(f"其中尾部找不到 startxref 一类的：{fatal_n} 个 → WPS 这类严格读法会报损坏")
        disagree = [r for r in results if not r["agree"]]
        if disagree:
            print(f"\n各内核结论不一致的 {len(disagree)} 个（宽容内核放行、严格判坏的就是这批）：")
            for r in disagree[:8]:
                one = "，".join(f"{e['engine']}={LEVEL_MARK.get(e['level'], '?')}{e['code']}"
                               for e in r["engines"] if e["code"] != "-")
                print(f"  {os.path.basename(r['path'])}：{one}")
    elif not args.quiet:
        print(f"用时 {human_secs(secs)}", flush=True)
    if args.csv:
        path = os.path.abspath(args.csv)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["结论码", "等级", "致命指纹", "各内核结论", "大小", "页数", "用时s", "路径", "警告/细节"])
            for r in sorted(results, key=lambda x: (LEVEL_ORDER[x["level"]], -x["size"])):
                one = "；".join(f"{e['engine']}={e['code']}" for e in r["engines"] if e["code"] != "-")
                det = " | ".join("；".join([e["note"]] + (e.get("warns") or []))
                                 for e in r["engines"] if e["code"] != "-")
                wr.writerow([r["code"], r["level"], "是" if r.get("fatal") else "", one,
                             r["size"], r.get("pages") or "", f"{r['t']:.2f}", r["path"], det[:1200]])
        print(f"报告已写出：{path}")
    if args.list_file:
        bad = [r["path"] for r in results if r["level"] == BROKEN]
        with open(os.path.abspath(args.list_file), "w", encoding="utf-8") as f:
            f.write("\n".join(bad) + ("\n" if bad else ""))
        print(f"损坏清单：{os.path.abspath(args.list_file)}（{len(bad)} 条）")
    return 1 if cnt[BROKEN] else 0


if __name__ == "__main__":
    sys.exit(main())
