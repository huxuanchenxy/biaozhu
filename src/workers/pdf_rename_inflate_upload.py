#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_rename_inflate_upload.py —— 批量：MinIO 里的 PDF 重命名(_sssss) + 膨胀 + 回传原目录

★ 自包含单文件（Windows / Linux 通用）
  - 不 import 同目录的 pdf_download / pdf_inflate：所需的 SigV4 签名、列目录、进度条、合法
    尾部填充已全部内联进本文件，把这一个 .py 拷到 Linux 机器上就能直接跑，零第三方依赖。
  - 桶连接信息与目标 PDF 目录写在下面的「配置区 CONFIG」里，脚本只靠本文件即可运行，
    同级目录不需要任何配置文件；命令行参数与环境变量仍可临时覆盖（换桶/换目录而不改代码时用）。

流程（对成品语料目录下每个 PDF，记原件为 X.pdf）：
  1) 下载原件 X.pdf 到内存（不落盘：本地不写任何 PDF，避免占用磁盘）
  2) 本地按规则膨胀，生成变大后的字节（合法尾部填充 + 补一份 startxref/%%EOF 结尾，PDF 仍可正常打开）
  3) 上传原件为 X_sssss.pdf（“重命名”语义：原件以 _sssss 名字保留，不删不改数据）
  4) 上传膨胀版覆盖回 X.pdf（无 _sssss 的“扩大过后的文件”落在原目录原名下）
  5) 本地追加一份处理流水日志 .log（记录桶内已生成 X_sssss.pdf 的对象）

膨胀规则（按原始大小，单位 MiB；档位内带随机小数，不再整齐落在 8M/9M/10M）：
  < 1M → 8.1~8.9M    1M~2M → 9.1~9.9M    2M~5M → 10.1~10.9M    > 5M → 规则外，跳过不动
  （最后那条上限可用 --max-mb 调大，给“拿点名的超尺寸文件单独验证”用；全目录跑时别动）

上传前校验（默认开，--verify off 可关）：
  覆盖上传前先在内存里问一次「这份字节阅读器能不能打开」，判坏的不上传。
  装了 PyMuPDF（pip install pymupdf）就按真内核判；没装自动降级成零依赖的「尾部指针判定」
  （只查尾部 1KB 窗口内有没有一条可用的 startxref——恰好就是旧规则产物被判坏的那一条），
  启动横幅里会写明用的哪种口径，不假装校验过。
  拦不拦看「双检」结果：
    产物坏而原件不坏 → 是我们改坏的，拦住不上传（连 _sssss 也不建，下次重跑还会重试）；
    原件本身就坏 → 源件毛病（膨胀只追加不改写，这类 refix 也修不好），照常上传并记进 source_bad，
                  不让一个源件问题卡住整批。
  判据三档绝不混（都是实测出来的）：
    致命→判坏：尾部指针根本不可用。bad.pdf 属此：真指针被推到距尾 9.4MB（尾部回搜 1KB 找不到），
            WPS 报损坏，而浏览器/pdf.js 自己扫全文重建所以看着正常——只按“能不能打开”判会全部漏报。
    需重建→可疑：内核重建了 xref 才读到，但尾部指针在位、页码齐全。04A11310…/04801490…（iText
            增量更新件）属此：告警是 expected trailer marker + trying to repair broken xref，
            而 startxref 距尾只 23 字节、指向确实是 xref 表，WPS 实测正常打开——拦它就是把好件判废。
    琐碎→可疑：书签树坏节点、缺字形回退字体。15DZ2260300-01.pdf 只有这一条告警、WPS 实测能打开，
            跟着判坏会大面积误杀，所以默认只记录不拦（要连可疑也拦：--verify-strict）。
  日志里不区分后面两档：只写「正常 / 打不开」——能开就算正常，只有致命档才写打不开。

用法（Windows 用 python，Linux 用 python3 或 ./本文件）：
  python pdf_rename_inflate_upload.py -n 10            # 干跑（默认）：只探测+算目标，不改动桶
  python pdf_rename_inflate_upload.py -n 10 --apply    # 真正处理 10 个（只算还没处理的）
  python pdf_rename_inflate_upload.py -n 0 --apply     # 处理全部未处理（慎用）
  python pdf_rename_inflate_upload.py -n 200 -w 4 -q   # 4 线程并发处理 200 个（推荐：进度条不交错）
  python pdf_rename_inflate_upload.py -n 5 --refix --apply   # 重做已处理过的：从 _sssss 取原件按新规则重覆盖
  python pdf_rename_inflate_upload.py -n 20 --apply --verify tail     # 不装库也拦住「尾部没 startxref」那批
  python pdf_rename_inflate_upload.py -n 20 --apply --verify-strict   # 连「可疑」也不上传（默认只拦判坏的）
  python pdf_rename_inflate_upload.py -n 20 --apply --verify off      # 关掉校验（老行为）
  python pdf_rename_inflate_upload.py --refix --refix-only -n 0 -w 8 -q    # 只重做有 _sssss 备份的，新文件不碰
  python pdf_rename_inflate_upload.py --refix --refix-only -n 0 -w 8 -q --scan-first   # 旧行为：先探完再开工（干跑摸底总数用）

  # 只重做点名的某几个文件：把名字填进本文件 CONFIG["FILES"]（写 X.pdf 或 X_sssss.pdf 都算同一个）
  python pdf_rename_inflate_upload.py --files "AP210A.pdf,AP211B_sssss.pdf"        # 干跑看计划
  python pdf_rename_inflate_upload.py --files "AP210A.pdf" --apply                 # 只动这一个
  python pdf_rename_inflate_upload.py --files-from bad.txt --apply                 # 名单来自体检报告
  python pdf_rename_inflate_upload.py --files-from "pdf_processed_*.log"           # 名单也支持通配（合并历史几轮）

  # 日志：默认每轮都写一份新文件，文件名自动插上本次运行时间戳（启动横幅会打印完整路径）
  python pdf_rename_inflate_upload.py -n 5 --apply                    # → pdf_processed_20261029145550.log
  python pdf_rename_inflate_upload.py -n 5 --apply --log-ts off       # 固定文件名、一直往一份里追加
  python pdf_rename_inflate_upload.py -n 5 --apply --log my.log       # 自取名 → my_20261029145550.log（仍插时间戳）
  python pdf_rename_inflate_upload.py -n 5 --apply --log ""           # 不写流水日志（Linux；Windows PowerShell 会把
  python pdf_rename_inflate_upload.py -n 5 --apply --log              #   空串参数吞掉，只写开关不带值就是同一个意思）
  #   日志内容：正文每个成功文件一行 TSV（最后一列是校验结论「源件…/产物…」）；失败/被拦下的
  #   文件也落一行（状态列 FAILED / BLOCKED，拿这份日志当 --files-from 名单就能定点重试）；
  #   末尾整段追加本轮汇总页，每行以 # 开头（当名单读时会被当注释跳过）。

  # Linux（拷到 /opt/tools 后）：
  chmod +x pdf_rename_inflate_upload.py
  ./pdf_rename_inflate_upload.py -n 20 -w 4 -q              # 先干跑看计划
  ./pdf_rename_inflate_upload.py -n 20 -w 4 -q --apply      # 小批量试点
  nohup ./pdf_rename_inflate_upload.py -n 0 -w 8 -q --apply > run.out 2>&1 &   # 后台全量
  # 临时换桶/换目录（不改代码，命令行优先级最高）：
  PDF_PREFIX='bucket/别的/目录' MINIO_ENDPOINT=http://1.2.3.4:9000 ./pdf_rename_inflate_upload.py -n 5
  # 多机分摊同一目录（关键：除 --shard 外各机参数必须一致）：
  #   机器1: ./pdf_rename_inflate_upload.py --shards 5 --shard 0 -n 0 -w 4 -q --apply
  #   机器2: ./pdf_rename_inflate_upload.py --shards 5 --shard 1 -n 0 -w 4 -q --apply
  #   ... 机器5: ... --shard 4 ...

并发与健壮性：
  - 默认干跑，不写桶；显式 --apply 才改动。
  - 全程在内存处理，不把 PDF 下载到本地磁盘（无残留、不占空间）；仅在本地写 .log 流水日志，
    默认每轮一份新文件（文件名插本次运行时间戳），因为这份日志同时是 --files-from 的名单来源，
    几轮追加进同一份会越滚越大、也分不清哪批是哪一轮做的；--log-ts off 可退回固定名追加。
  - 防重复处理：X_sssss.pdf 存在=已完成则跳过；膨胀版（已达原件尺寸上限）不会被再次膨胀；
    产物 _sssss 不作为源。
    （对同一目录重复跑只会处理“还没做过”的文件；随机档位仅落在新文件上，不动已完成的产品。）
    例外：显式 --refix 才重做已完成文件（拿 sidecar 里的原件重新膨胀覆盖 X.pdf，sidecar 本身不动）。
    注意 --refix 的预筛口径是 new/repair/refix 三类，会顺带把还没处理过的新文件也首次处理掉；
    只想重做已有备份的那批、不让范围扩大，再加 --refix-only（没有 _sssss 备份的一律不碰）。
  - 定向名单（CONFIG["FILES"] / --files / --files-from / PDF_FILES）：配了就只处理名单里这几个，
    不扫全目录、自动按「重做」语义走（名字写 X.pdf 还是 X_sssss.pdf 都归一到同一份）。
  - 边扫描边处理（默认）：HEAD 扫到一个待处理项就立刻投给处理线程池，不再等整份目录探完
    才开工（几十万对象时那一步要空等几十分钟、且一个字节都没搬）。扫描只发 HEAD 很轻，
    几个线程就够（--scan-workers 默认取处理线程数的一半）；处理侧用信号量背压，在途+排队
    有限，不会堆几十万个任务，Ctrl+C 也能很快收尾。
    加 --scan-first 回到旧行为（先探完整份目录再开工）：干跑想一眼看清全集总数时用它。
    -n 语义不变：凑够 limit 个待处理就停止扫描（已投喂的照做完），所以它是“本轮最多做多少个”；
    想覆盖全集就 -n 0。列目录仍默认全量翻页（截断列表就找不到排在后段的文件）。
  - 行首 [n/m]：流水线模式下 m 是“提交那一刻已发现的待处理数”，边扫边长，不是最终总数；
    --scan-first 与定向名单模式下 m 是确定的总数。
    另外：并行 HEAD 谁先回就先投，所以流水线里的处理顺序只是「大致按名称序」，不再严格
    等于字典序（对结果没影响：每个文件独立且幂等）；只有 -n 截断时“具体做到哪几个”
    可能与旧行为差一两行，要完全可复现的顺序就加 --scan-first。
  - 单进程多线程：-w/--workers 指定并发线程数（默认取 CONFIG['WORKERS']）。下载/上传是网络
    I/O 瓶颈，多线程可明显提快；线程共用同一签名客户端，统计/日志/输出均加锁。建议 -w 4~8，
    过大容易撞服务端限流(429)，脚本会自动退避重试。
  - 多机并发安全：
      · --shard/--shards 让各机处理互不相交的子集（基于稳定排序的 index）。
      · 上传原件 X_sssss.pdf 用条件写（If-None-Match:*）做“原子认领”，只有成功者才覆盖 X.pdf，
        失败(412)者跳过，避免重复覆盖与把膨胀版误当原件的污染。
      · 网络/5xx/429 自动重试 + 指数退避；412/404 不重试；时钟偏差自动校正重签。
      · 崩溃自愈：X_ssssss 已存在但 X.pdf 仍小（上次败在覆盖前）→ 从 _sssss 取原件补做覆盖(repair)。
  - 服务端若不支持条件写会自动回退为普通写（此时强烈建议配合 --shard 分片保证不重叠）。
  - 每文件分四步计时，结尾输出总数、各步骤累计耗时、数据量与整体用时；这段汇总会原样
    追加到日志末尾（窗口关了/机器换了也能查到这一轮干了多少）。
  - 上传前校验（默认开）：判据与双检策略见上面「上传前校验」一节。日志只有一份：校验结论不单独
    落文件，就接在流水日志每行最后那一列，只说两件事——源件（_sssss 原件）正常/打不开、膨胀过的
    文件正常/打不开（能打开就算正常，不写告警流水）；只有打不开才附一句短因。
    用内核口径时，从清警告到取警告整段上锁串行（fitz 的警告收集器是进程级全局，多线程会互相
    串味）；单次 open 只几十毫秒，相对上传几 MB 的秒级耗时可以忽略。
  - 静默也有心跳：-q 只关单文件进度条，列目录/扫描这些不下载不上传、容易看着像卡死的阶段，
    仍每隔 5 秒打一行整行进度（已探测 x/N、待处理几个、已处理完几个、约剩多久），流水线
    模式下探测速度等于处理速度，所以那个“约剩多久”就是整个任务的预估完成时间；
    nohup/重定向到文件里也照样留痕；间隔可用环境变量 PDF_HEARTBEAT 调（秒）。

Linux/Windows 兼容要点：
  - 纯标准库；Python 3.6+（建议 3.8+）。
  - 文件为 LF 行尾，因此 chmod +x 后可直接 ./ 执行；若在 Windows 上被改回 CRLF，shebang 会报
    “/usr/bin/env: 'python3\r': No such file or directory”，此时用 `python3 本文件.py` 执行（不受
    行尾影响），或先 `sed -i 's/\r$//' 本文件.py` 去掉 CR。
  - 启动时把 stdout/stderr 切成 UTF-8（errors=replace），LANG=C/POSIX 的容器里打印中文与 ✓✗
    不会抛 UnicodeEncodeError；控制台确实编不出时逐行降级为替换字符或 [OK]/[X]。
  - 对象 key 一律按 '/' 处理（MinIO 路径规则），不依赖 os.sep；本地日志用 os.path.join，
    且相对路径日志固定落在脚本同目录，nohup/systemd 任意工作目录都能找到同一份流水日志。
  - Ctrl+C 时线程池「不再排新任务」，在途文件收尾后输出统计；老版本 Python 无 cancel_futures
    时自动降级。
"""

import argparse
import datetime
import glob
import hashlib
import hmac
import io
import os
import random
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
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

if sys.version_info < (3, 6):
    sys.exit("需要 Python 3.6 及以上（当前：%s）" % sys.version.split()[0])

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== 配置区：Linux 上只靠这里就能跑（无需 .env） ====================
CONFIG = {
    # ---- MinIO 连接（path-style 直连；endpoint 末尾不要带 /）----
    "MINIO_ENDPOINT": "http://10.89.14.234:12001",
    "MINIO_ACCESS_KEY": "NXKKBDFEZP9XNNHVRIHT",
    "MINIO_SECRET_KEY": "Fx0k0Ix2Pab4hkZMOvl3oLLKL08W5ZPVmBiTyJUS",
    # ---- 待处理目录前缀：<bucket>/<目录路径>（只列这一层下的 *.pdf）----
    "PREFIX": "drivdernet_abc/自筹/5_材料基因库/5.1_通用材料基因数据/成品语料",
    # ---- 定向名单：一旦配置就「只处理这几个文件」，不再扫全目录（留空=按目录处理）----
    #   只写文件名即可（不用带目录前缀），下面三种写法都认、可混用：
    #     "AP210A.pdf"          原件名
    #     "AP210A_sssss.pdf"    产物名（从桶里复制出来的多半是这个名字），自动归一到 AP210A.pdf
    #     "AP210A"              省略 .pdf
    #   语义：名单即范围——--shards/--shard/--list-cap 被忽略，-n 默认=名单全做；
    #   已膨胀过的默认按「重做」处理（等价 --refix：从 _sssss 取原件按当前规则重新膨胀
    #   覆盖 X.pdf，_sssss 本身不动），没处理过的按新规则正常处理，找不到/没法重做的逐条说明。
    #   临时覆盖：命令行 --files "A.pdf,B_sssss.pdf"，或环境变量 PDF_FILES="A.pdf,B.pdf"。
    "FILES": [],
    #   名单也可以放文本文件里（每行一个文件名，# 开头为注释；这里的路径按本脚本目录解析，
    #   命令行 --files-from 给的路径则按当前工作目录解析）。本脚本自己的流水日志
    #   pdf_processed_*.log（TSV，每轮一份带时间戳的新文件）也能直接当名单用：自动取状态列
    #   后面那个原件名并去重，于是「只重做已建过 _sssss 备份的那批」不用列全目录（列 11 万个
    #   对象很费时间）；--files-from "pdf_processed_*.log" 可按通配合并历史几轮。
    #   直接把 pdf_check_damaged.py 的 --list-file 产出甚至 CSV 报告行粘进来也能识别。
    #   临时覆盖：--files-from bad.txt（只写参数不带值=禁用）。
    "FILES_FROM": "",
    # ---- 签名区域/服务：MinIO 一般保持 us-east-1 / s3 即可 ----
    "AWS_REGION": "us-east-1",
    "AWS_SERVICE": "s3",
    # ---- 本地流水日志（相对本脚本目录；空字符串=禁用）----
    #   LOG_TS=True（默认）时实际写出的文件名会插上本次运行时间戳：
    #     pdf_processed.log → pdf_processed_20261029145550.log   每轮一份新文件
    #   这是本轮唯一的日志：上传前校验的结论就在每行最后一列，不再第二份文件。
    "LOG_FILE": "pdf_processed.log",
    "LOG_TS": True,               # False 或 --log-ts off = 沿用固定文件名、逐轮追加
    # ---- 上传前校验：产物到底能不能被阅读器打开（判据见下面「上传前校验」一节的注释）----
    #   "auto"：装了 PyMuPDF 就按内核判（pip install pymupdf），没装自动降级成零依赖的尾部指针判定
    #   "tail"：强制只用零依赖判定（想保住本文件的零依赖跑法时用）；"off"：完全不校验
    "VERIFY": "auto",
    "VERIFY_STRICT": False,        # True：连「可疑」（书签树/回退字体一类琐碎告警）也拦下不上传
    # ---- 原件最大可处理尺寸（MiB）：超过它且没有 _sssss 备份的一律规则外跳过 ----
    #   只为「拿点名的超尺寸文件单独验证」才调大（--max-mb）；放宽后已膨胀的完成版
    #   会被当原件再膨一次，别在全目录模式下动它。
    "MAX_MB": 5,
    # ---- 默认并发线程数 / 单请求重试次数 / 单请求超时（秒）----
    "WORKERS": 4,
    "RETRIES": 4,
    "TIMEOUT": 180,
}
# 覆盖优先级：命令行参数 > 环境变量 > 上面的 CONFIG > 项目 .env（仓库内临时换参数用）。
#   环境变量名：MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / PDF_PREFIX / PDF_FILES
#               / PDF_VERIFY / PDF_VERIFY_STRICT / PDF_LOG_TS / PDF_MAX_MB
# 本文件自带连接信息，单独拷到 Linux 机器上就能跑，同级目录不需要任何配置文件。

REGION = CONFIG["AWS_REGION"]
SERVICE = CONFIG["AWS_SERVICE"]
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TIMEOUT = int(CONFIG["TIMEOUT"])
_MIB = 1024 * 1024
_BIG = 5 * _MIB          # 分界：可处理原件 <5M，膨胀完成版至少 ~8M；超过且无 _sssss 视为规则外/已完成


def set_big_limit(mib) -> None:
    """调大「原件最大可处理尺寸」（默认 5MiB，--max-mb / PDF_MAX_MB）。
       只为拿点名的超尺寸文件做单独验证；全目录模式下放宽会让已膨胀的完成版
       被当成原件再膨一次，别随便调。非法值/≤ 0 一律忽略，保持原值。"""
    global _BIG
    try:
        v = float(mib)
    except (TypeError, ValueError):
        return
    if v > 0:
        _BIG = int(round(v * _MIB))


# ---------------- 配置读取（CONFIG / 环境变量 / .env 兼容） ----------------

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
    """收集本机可能存在的 .env（脚本同目录 + 仓库内的项目根），仅作兜底，缺失不报错（结果缓存）。"""
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
    """按「命令行 > 环境变量 > CONFIG > .env」取第一个非空值。"""
    for v in (cli_value, os.environ.get(env_name or name), CONFIG.get(name)):
        if v not in (None, ""):
            return str(v).strip()
    dot = _dotenv_values()
    for k in (dotenv_keys or (name, "VITE_" + name)):
        if dot.get(k):
            return str(dot[k]).strip()
    return ""


def resolve_endpoint_ak_sk(args) -> tuple:
    """返回 (endpoint, ak, sk)；配置缺失时给出可操作报错。"""
    endpoint = _pick("MINIO_ENDPOINT", args.endpoint)
    ak = _pick("MINIO_ACCESS_KEY", args.access_key)
    sk = _pick("MINIO_SECRET_KEY", args.secret_key)
    if not endpoint or not ak or not sk:
        sys.exit("错误：MinIO 连接参数不完整。请在本文件 CONFIG 里填 MINIO_ENDPOINT / "
                 "MINIO_ACCESS_KEY / MINIO_SECRET_KEY，或用命令行 --endpoint/--access-key/"
                 f"--secret-key、环境变量传入（当前 endpoint={bool(endpoint)},"
                 f"ak={bool(ak)},sk={bool(sk)}）")
    return endpoint.rstrip("/"), ak, sk


def resolve_prefix(args) -> str:
    prefix = _pick("PREFIX", args.prefix, env_name="PDF_PREFIX",
                   dotenv_keys=("MINIO_PREFIX",))
    if not prefix or "/" not in prefix:
        sys.exit(f"错误：目录前缀需为 <bucket>/<路径>，当前：{prefix!r}"
                 "（改本文件 CONFIG['PREFIX']，或 --prefix / 环境变量 PDF_PREFIX）")
    return prefix


# ---------------- 终端编码与路径工具 ----------------

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
    """保证打印不会因编码问题炸掉，同时不在 GBK 控制台上强行改 UTF-8（那会乱码）：
      - 能编中文的控制台（Windows cp936/GBK、已设 UTF-8 的 Linux）：保持原编码，只把
        错误处理设成 replace，遇到编不出的字符降级而不是抛异常。
      - LANG=C/POSIX 的 Linux 容器（stdout 走 ANSI_X3.4-1968/ascii）：编不出中文，切 UTF-8
        （现代终端能显示，重定向到文件也便于事后 grep）。
    """
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
        if not switch:                              # 老版本且原编码可用：交给 log() 逐行兜底
            continue
        try:
            wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
        except Exception:
            continue
        setattr(sys, name, wrapped)


def _strict_encode(seg: str) -> str:
    """单段严格编码（RFC3986：encodeURIComponent 再补 !'()*），与签名 canonical 保持一致。"""
    return urllib.parse.quote(seg, safe="-_.~")


def strict_encode_path(p: str) -> str:
    """路径逐段规范化：先 decode 再严格编码，保证「线上路径 == canonical URI」。"""
    return "/".join(_strict_encode(seg) for seg in p.split("/"))


def _amz_date(dt: datetime.datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _canonical_query(params: dict) -> str:
    items = sorted((_strict_encode(k), _strict_encode(v)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def skew_offset(err_body: bytes, err_headers):
    """若响应体是 RequestTimeTooSkewed，按 MinIO 的 Date 头算出秒级时钟偏差；否则 None。"""
    if b"RequestTimeTooSkewed" not in err_body:
        return None
    from email.utils import parsedate_to_datetime
    date_str = err_headers.get("Date") if err_headers else None
    if not date_str:
        return None
    try:
        server = parsedate_to_datetime(date_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        return int((server - now).total_seconds())
    except Exception:
        return None


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
    """单文件传输进度条：[名] |██░░| 12.3MB/45.6MB 27.0% 3.1MB/s 用 4s。非 TTY 自动静音。"""

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


# 静默模式（-q）下的心跳间隔（秒）：环境变量 PDF_HEARTBEAT 可临时改（比如设 10 更清净）
try:
    _HEARTBEAT = max(1.0, float(os.environ.get("PDF_HEARTBEAT") or 5.0))
except ValueError:
    _HEARTBEAT = 5.0


class Heartbeat:
    """静默模式下的粗粒度心跳进度。

    为什么要它：列目录（4 万个对象要翻几十页）与预筛（每个文件 2 次 HEAD，共约 8 万次
    请求）这两步不下载不上传，原本只在非静默时用 \r 原地刷新；一旦加了 -q（或 nohup 把
    输出重定向进文件，\r 几行挤在一行根本看不清）就变成好几分钟一声不吭，看着像卡死。
    这里在静默模式下每隔 _HEARTBEAT 秒打一整行，带已用时间与预估剩余；非静默不插手，
    沿用调用方原有的原地刷新。
    """

    def __init__(self, label: str, quiet: bool, total: int = 0):
        self.label = label
        self.quiet = quiet
        self.total = total
        self.t0 = time.time()
        self.last = 0.0
        self.done = 0

    def tick(self, done: int, note: str = ""):
        """done=已完成计数。只在静默模式下输出，并且受心跳间隔限流。"""
        self.done = done
        if not self.quiet:
            return
        now = time.time()
        if now - self.last < _HEARTBEAT:
            return
        self.last = now
        eta = ""
        if self.total and done > 0:
            rate = done / max(now - self.t0, 1e-9)
            if rate > 0:
                eta = f"，约剩 {human_secs(max(0, self.total - done) / rate)}"
        log(f"  [{human_secs(now - self.t0)}] {self.label} {done}"
            + (f"/{self.total}" if self.total else "")
            + (f"（{note}）" if note else "") + eta)


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
    hb = Heartbeat("列目录中，已发现 PDF", quiet)
    if not quiet:
        print(f"正在列目录：{prefix} …（大目录可能较慢，已限制最多扫 {max_pdf or '不限'} 个）", flush=True)
    else:
        log(f"列目录中：{prefix} …（对象多要翻很多页；静默模式下每 {int(_HEARTBEAT)}s 报一行，"
            "没输出不等于卡住）")
    pathname = strict_encode_path(f"/{bucket}")
    keys, token, offset, full = [], None, 0, False
    while True:
        params = {"list-type": "2", "encoding-type": "url", "max-keys": "1000",
                  "prefix": key_prefix, "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        try:
            resp = _signed_get(endpoint, ak, sk, pathname, params, offset_sec=offset)
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
        hb.tick(len(keys))
        if not quiet:
            print(f"列目录中：已找到 {len(keys)} 个 PDF…", end="\r", flush=True)
    keys.sort()
    total_msg = f"目录共 {len(keys)} 个 PDF（不含子目录）" if full \
        else f"已列到 {len(keys)} 个 PDF（目标目录对象很多，按需提前截断）"
    if not quiet:
        print(" " * 60, end="\r") if not full else None
        print(total_msg)
    else:
        log(f"  [{human_secs(time.time() - hb.t0)}] {total_msg}")
    return keys


def _signed_get(endpoint, ak, sk, pathname, params, offset_sec=0):
    """签名并发起 GET（列目录用）；返回响应对象，调用方负责读取/关闭。"""
    now = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(seconds=offset_sec))
    query = _canonical_query(params)
    headers = _build_auth(endpoint, ak, sk, "GET", pathname, query, EMPTY_SHA256, now)
    url = endpoint + pathname + ("?" + query if query else "")
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=_TIMEOUT)


def _build_auth(endpoint, ak, sk, method, pathname, query, payload_hash, now):
    """SigV4 Authorization 等头部（host + x-amz-date + body sha256）。"""
    host = urllib.parse.urlparse(endpoint).netloc
    amz = _amz_date(now)
    ds = amz[:8]
    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{payload_hash}\nx-amz-date:{amz}\n")
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, pathname, query, canonical_headers, signed_headers, payload_hash])
    scope = f"{ds}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])

    def h(key, s):
        return hmac.new(key, s.encode(), hashlib.sha256).digest()

    signing = h(h(h(h(f"AWS4{sk}".encode(), ds), REGION), SERVICE), "aws4_request")
    signature = hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "x-amz-date": amz,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }


# ---------------- 合法尾部填充（原 pdf_inflate.write_padding） ----------------

# 每行注释的字节数。故意不用旧版的一整块 8MB：单行超长注释会让部分阅读器/解析器
# 判定为非法 PDF（整块里也没有任何换行），折成几 KB 一行更像正常 PDF 内部结构。
_PAD_LINE = 4 * 1024
_FILL_BYTE = b"\0"           # 填充用的重复字节（可打印字符便于识别）
_PAD_FOOTER = b"%inflate-end\n%%EOF\n"    # 填充块结束标记


def write_padding(stream, nbytes: int):
    """以注释块形式向 PDF 尾部写入 nbytes 填充数据（开头自带换行，避免与 %%EOF 粘连）。

    实际写入总量（含结束标记）恒为 nbytes + len(_PAD_FOOTER)，便于精确预估进度。
    """
    if nbytes <= 0:
        return
    header = b"\n%inflate-begin(" + str(nbytes).encode() + b")\n"
    stream.write(header)
    remaining = nbytes - len(header)
    while remaining > 0:
        size = min(_PAD_LINE, remaining)
        if size >= 2:
            # 1 字节 % + (size-2) 填充 + 1 字节换行 = 恰好 size 字节
            stream.write(b"%" + _FILL_BYTE * (size - 2) + b"\n")
        else:  # 只剩 1 字节：用换行补齐，保证总量准确
            stream.write(b"\n")
        remaining -= size
    stream.write(_PAD_FOOTER)


# ---------------- 直连 MinIO 客户端：HEAD / GET / PUT / 条件 PUT（SigV4 + 重试 + 时钟校正） ----------------

def _retryable(code: int) -> bool:
    return code in (408, 425, 429) or code >= 500


class Minio:
    def __init__(self, endpoint: str, ak: str, sk: str, retries: int = 4, no_cond: bool = False):
        self.endpoint = endpoint
        self.ak = ak
        self.sk = sk
        self.host = urllib.parse.urlparse(endpoint).netloc
        self.offset = 0          # 与 MinIO 的时钟偏差（秒）
        self.retries = max(1, retries)
        self.no_cond = no_cond   # 是否禁用条件写（If-None-Match）
        self._cond_warned = False

    def _sign(self, method, pathname, query, payload_hash, now):
        return _build_auth(self.endpoint, self.ak, self.sk, method, pathname,
                           query, payload_hash, now)

    def _send(self, method, key, params=None, data=None, extra=None):
        """单次签名请求；不做重试。4xx 抛 HTTPError 交由上层判断。"""
        payload_hash = (hashlib.sha256(data or b"").hexdigest()
                        if method in ("PUT", "POST") else EMPTY_SHA256)
        pathname = strict_encode_path("/" + key)
        query = _canonical_query(params or {})
        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.offset)
        headers = self._sign(method, pathname, query, payload_hash, now)
        if extra:
            headers.update(extra)
        url = self.endpoint + pathname + (("?" + query) if query else "")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=_TIMEOUT)

    def _call(self, method, key, params=None, data=None, extra=None):
        """带重试与时钟校正的调用；不可重试错误（含 412）直接抛 HTTPError。"""
        for attempt in range(self.retries):
            try:
                return self._send(method, key, params, data, extra)
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                off = skew_offset(body, e.headers)
                if e.code == 403 and off is not None and self.offset == 0:
                    self.offset = off          # 校正时钟后立即重试
                    continue
                if _retryable(e.code) and attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
            except (urllib.error.URLError, OSError) as e:
                if attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise RuntimeError(f"{method} {key} 网络失败：{e}") from e

    def head_size(self, key: str) -> int:
        r = self._call("HEAD", key)
        n = int(r.headers.get("Content-Length") or 0)
        r.close()
        return n

    def exists(self, key: str) -> bool:
        try:
            self.head_size(key)
            return True
        except Exception:
            return False

    def get_bytes(self, key: str, prog: Progress = None) -> bytes:
        resp = self._call("GET", key)
        total = int(resp.headers.get("Content-Length") or 0)
        if prog is not None:
            prog.total = total
        buf, done = bytearray(), 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            buf += chunk
            done += len(chunk)
            if prog is not None:
                prog.update(len(chunk))
        resp.close()
        return bytes(buf)

    def put_bytes(self, key: str, data: bytes, prog: Progress = None):
        resp = self._call("PUT", key, data=data)
        resp.read()
        resp.close()
        if prog is not None:
            prog.update(len(data))

    def put_conditional(self, key: str, data: bytes) -> bool:
        """条件写创建对象：不存在才成功。返回是否成功认领（True=我创建/已回退写入）。
        412（已存在）返回 False。服务端不支持条件写时自动回退普通写并返回 True。"""
        if self.no_cond:
            self.put_bytes(key, data)
            return True
        try:
            resp = self._call("PUT", key, data=data, extra={"If-None-Match": "*"})
            resp.read()
            resp.close()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 412:                 # 已存在：他人已认领/已处理
                return False
            if e.code in (400, 405, 501) and not self._cond_warned:
                # 服务端不支持条件写：回退普通写（提示配合 --shard 保证不重叠）
                self._cond_warned = True
                print("警告：MinIO 不支持条件写(If-None-Match)，已回退普通上传；"
                      "多机并发请配合 --shard/--shards 分片避免重叠", file=sys.stderr)
                self.no_cond = True
                self.put_bytes(key, data)
                return True
            raise


# ---------------- 膨胀规则与实现 ----------------

# 目标档位（原始大小 → 目标整数部分 MiB）：<1M→8、1M~2M→9、2M~上限→10、超过上限不处理。
# 目标在整数部分之上叠随机小数(_JITTER)，让产物不再是清一色的 8M/9M/10M，
# 而是 8点几 / 9点几 / 10点几。
_JITTER_LO, _JITTER_HI = 0.1, 0.9   # 随机小数范围（MiB）


def _band_target(base_mib: int) -> int:
    """档位基值 + 随机小数，返回字节目标（保留档位整数部分，小数落在 _JITTER 区间）。"""
    return int(round((base_mib + random.uniform(_JITTER_LO, _JITTER_HI)) * _MIB))


def target_for(size: int):
    """按原始大小返回目标膨胀大小（字节，档位内随机）；超过 _BIG（默认 5M）返回 None。

    上限被 --max-mb 放宽后，大原件取「最大档 10.x」与「原件 + 1.xMiB」里更大的那个：
    既保证产物一定比原件大（否则填充块为空，白跑一轮），又不会一次胀出几十 MB。"""
    if size < 1 * _MIB:
        return _band_target(8)
    if size < 2 * _MIB:
        return _band_target(9)
    if size <= _BIG:
        return max(_band_target(10), size + _band_target(1))
    return None


# 只承认“文件结尾就是规范的 startxref + %%EOF”这一种情况（因而只看最后几十字节），
# 避免在二进制流里误命中“startxref”字样。
_TAIL_XREF_RE = re.compile(rb"startxref\s+(\d{1,20})\s+%%EOF\s*\Z")


def _tail_startxref(data: bytes):
    """取原文件结尾的 startxref 偏移，返回可追加的结尾字节（如 b'startxref\\n12345\\n%%EOF\\n'）。

    为什么要再补一份结尾：阅读器普遍从文件尾向前找 %%EOF，再在附近窗口里找 startxref。
    只在尾巴上堆几 MB 注释、却没有新的 startxref 时，真正的 xref 被推到回搜窗口之外，
    部分阅读器就报“文件已损坏/须要修复”甚至直接打不开。填充后原样补一份 startxref+%%EOF
    （偏移不变，因为我们只追加不改写），文件结尾看上去就跟一次正常的增量更新一样。
    结尾不规则（本身就坏/带附件）时返回 None，保持原来的纯追加行为，不会注入错误偏移。
    """
    m = _TAIL_XREF_RE.search(data[-64:])
    if not m:
        return None
    off = int(m.group(1))
    if off <= 0 or off >= len(data):     # 偏移不合理：不补，避免把阅读器引到错位置
        return None
    return b"startxref\n" + str(off).encode() + b"\n%%EOF\n"


def inflate_bytes(data: bytes, target: int) -> tuple:
    """在原字节尾部追加合法填充，逼近 target（扣除填充结束标记与补尾 startxref 的长度，
    让成品体积贴近 target）。返回 (新字节, 计划追加字节)。"""
    tail = _tail_startxref(data) or b""
    add = max(0, target - len(data) - len(_PAD_FOOTER) - len(tail))
    bio = io.BytesIO()
    bio.write(data)
    write_padding(bio, add)
    if tail:
        bio.write(tail)
    return bio.getvalue(), add


def s_key_of(key: str) -> str:
    root, ext = os.path.splitext(key)
    return f"{root}_sssss{ext}"


# 产物后缀：_sssss 为现行，_smallsmall 为上一版，_s 为早期测试遗留；均不当原件处理，避免被二次膨胀
_ARTIFACT_SUFFIXES = ("_sssss", "_smallsmall", "_s")


def is_artifact(key: str) -> bool:
    root = os.path.splitext(os.path.basename(key))[0]
    return root.endswith(_ARTIFACT_SUFFIXES)


def is_pdf_bytes(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


# ---------------- 上传前校验：产物到底能不能被阅读器打开 ----------------
#
# 为什么加这一层：膨胀只在尾部追加，追加方式一旦让阅读器找不到 startxref，产出的就是
# 「浏览器凑合能看、WPS 报文件损坏」的废文件。以前只能人工逐个点开才发现（bad.pdf 就是这么
# 撞上的），现在覆盖上传前先在内存里问一次，判坏的不上传。
#
# 判据按实测校准，三档绝不能混（结论与 pdf_check_lib.py 同源）：
#   致命→拦：尾部指针根本不可用。bad.pdf 属此——真指针被推到距尾 9.4MB，
#          WPS 从尾部回搜 1KB 找不到就报损坏；浏览器/pdf.js 会自己扫全文重建所以看着没事。
#   需重建→只记录不拦：内核重建了 xref 才读到，但尾部 startxref 在位、页码齐全。iText
#          增量更新件属此（04A11310… 17 页 / 04801490… 10 页，告警为 expected trailer
#          marker + trying to repair broken xref，而 startxref 距尾只 23 字节且指向真 xref 表），
#          2026-10-29 已拿这两份原件加一份 CLEAN 对照在 WPS 里人工点验，三份全部正常打开
#          ——把它算成损坏就是误杀。
#   琐碎→只记录不拦：书签树坏节点、缺字形回退字体。15DZ2260300-01.pdf 只有这一条告警，
#          WPS 实测能正常打开；把它算成损坏会大面积误杀。
# 内核优先级：装了 PyMuPDF（pip install pymupdf）就按真内核判；没装（比如刚部署的 Linux）自动
# 降级成零依赖的「尾部指针判定」——同样能拦住把指针推走的那类产物，只是查不出别的坏法，
# 报告里会写明用的哪种口径，不会假装校验过。

_V_BAD, _V_SUSP, _V_OK, _V_SKIP = "bad", "susp", "ok", "skip"
_TAIL_WIN = 1024            # 阅读器从尾部向前回搜 %%EOF/startxref 的经验窗口（字节）
# 「严格阅读器会挂」的指纹：只收「尾部指针不可用」那一类。判定证据是 bad.pdf 与旧规则产物
# （指针被推走 / 偏移说谎），WPS 实测报损坏。
# 已经按实测从本表里踢出去两条：expected trailer marker、trying to repair broken xref（它们是
# 内核重建 xref 的流水，iText 增量更新件也报、但三份样本人工在 WPS 里全部正常打开，
# 当依据会大面积误杀）。
_FATAL_PAT = ("cannot find startxref", "cannot find the trailer", "cannot build xref",
              "xref offset invalid", "page type instead of xref", "cannot find pdf header")
# 含这些词说明毛病在 xref/页表层（而不是书签字体一类），可疑级别里单独给个编码方便事后统计
_XREF_REBUILD_PAT = ("xref", "trailer", "repairing", "rebuild")
_TAIL_XREF_ANY_RE = re.compile(rb"startxref\s+(\d{1,20})\s+%%EOF")

_verify_mode = "auto"       # auto=有 fitz 用内核、没有自动降级；tail=强制零依赖；off=不校验
_verify_strict = False      # True：连「可疑」（琐碎告警，实测 WPS 仍能打开）也拦
_fitz = None
_fitz_tried = False
_verify_lock = threading.Lock()     # fitz 的警告收集器是进程级全局，必须整段串行


def set_verify(mode="auto", strict: bool = False):
    global _verify_mode, _verify_strict
    _verify_mode = str(mode or "auto").lower()
    _verify_strict = bool(strict)


def _load_fitz():
    """按需导入 fitz；失败只提示一次，开不了就走降级路径，不让依赖问题打断整批任务。"""
    global _fitz, _fitz_tried
    if _fitz_tried:
        return _fitz
    _fitz_tried = True
    try:
        import fitz
        _fitz = fitz
    except Exception as e:
        _fitz = None
        if _verify_mode == "auto":
            log(f"提示：导入 PyMuPDF 失败（{type(e).__name__}: {str(e)[:80]}），上传前校验自动降级为"
                f"「尾部指针判定」；想升级成真正的阅读器口径：pip install pymupdf")
    return _fitz


def verify_desc() -> str:
    """启动横幅里那句「用哪种口径校验」，说人话，并如实暴露降级。"""
    if _verify_mode == "off":
        return "已关（--verify off）"
    if _verify_mode == "tail":
        return "尾部指针口径（--verify tail 强制零依赖）"
    if _load_fitz() is None:
        return "尾部指针口径（未装 PyMuPDF，pip install pymupdf 可升到内核口径）"
    return "内核口径（PyMuPDF 走没走修复分支 + 警告指纹）"


def verify_pdf(data: bytes) -> tuple:
    """返回 (level, code, note)，level ∈ bad/susp/ok/skip。只回答「喂给阅读器能不能打开」。
       不抛异常：校验自己出问题一律当可疑放行，宁多记一条也不因库 API 变动搞挂整批。"""
    if _verify_mode == "off":
        return _V_SKIP, "off", "校验已关闭"
    fitz = None if _verify_mode == "tail" else _load_fitz()
    if fitz is None:
        return _verify_tail(data)
    return _verify_fitz(fitz, data)


def _verify_fitz(fitz, data: bytes) -> tuple:
    try:
        with _verify_lock:
            for flag in ("mupdf_display_errors", "mupdf_display_warnings"):
                fn = getattr(fitz.TOOLS, flag, None)
                if callable(fn):
                    fn(False)
            reset = getattr(fitz.TOOLS, "reset_mupdf_warnings", None)
            if callable(reset):
                reset()
            collector = getattr(fitz.TOOLS, "mupdf_warnings", None)
            try:
                doc = fitz.open(stream=data, filetype="pdf")
            except Exception as e:
                return _V_BAD, "OPEN_FAIL", "内核直接打不开：%s: %s" % (type(e).__name__, str(e)[:120])
            try:
                warns = (collector() if callable(collector) else "") or ""
                if not warns and callable(getattr(doc, "mupdf_warnings", None)):
                    warns = doc.mupdf_warnings() or ""      # 个别老版本把警告挂在 Document 上
                repaired = bool(getattr(doc, "is_repaired", False))
                pages = doc.page_count
            finally:
                try:
                    doc.close()
                except Exception:
                    pass
    except Exception as e:
        return _V_SUSP, "CHECK_ERROR", "校验自身异常（%s），按可疑放行" % type(e).__name__
    lines = [w.strip() for w in warns.splitlines() if w.strip()]
    fatal = next((w for w in lines if any(k in w.lower() for k in _FATAL_PAT)), "")
    if fatal:
        return _V_BAD, "XREF_FATAL", "走修复分支且尾部指针不可用：%s（%s 页）" % (fatal[:120], pages)
    if repaired or lines:
        first = (lines[0][:120] if lines else "is_repaired=True 但无警告明细")
        if any(k in w.lower() for w in lines for k in _XREF_REBUILD_PAT):
            # 内核重建了 xref 才读到，但指针在位、页码齐全：实测 WPS 这类严格阅读器也能打开，
            # 所以只记不拦；真坏的那一类已经被上面 XREF_FATAL 接住了
            return _V_SUSP, "XREF_REBUILT", "内核重建了 xref（指针在位、页码齐全，不拦）：%s（%s 页）" % (
                first, pages)
        return _V_SUSP, "MINOR_WARN", "能打开，只有琐碎瑕疵：%s（%s 页）" % (first, pages)
    return _V_OK, "CLEAN", "%s 页，内核未走修复" % pages


def _verify_tail(data: bytes) -> tuple:
    """零依赖降级：只查尾部窗口内有没有一条位置合法的 startxref。
       这一条就够拦住旧规则那种「把真指针推到几 MB 之外」的产物；别的坏法查不出，
       所以结论里写清口径，不假装是内核判的。"""
    if not data:
        return _V_BAD, "EMPTY", "0 字节"
    if not is_pdf_bytes(data):
        return _V_BAD, "NOT_PDF", "开头没有 %PDF- 魔数"
    hits = _TAIL_XREF_ANY_RE.findall(data[-_TAIL_WIN:])
    if not hits:
        return _V_BAD, "TAIL_NO_STARTXREF", "尾部 %d 字节窗口内找不到 startxref" % _TAIL_WIN
    off = int(hits[-1])
    if off <= 0 or off >= len(data):
        return _V_BAD, "XREF_OFF_RANGE", "尾部 startxref 指向 %d，越出文件长度 %d" % (off, len(data))
    return _V_OK, "TAIL_OK", "尾部指针在窗口内且指向文件内（未装内核，只查了这一项）"


def _vshort(lv: str, cd: str, nt: str) -> str:
    """日志里那一小节：只回答能不能打开。能打开（连「可疑」也算）就一个字「正常」，
       不报告警流水；真打不开才附一句短因（取告警里冒号后那半句，最多 60 字）。"""
    if lv == _V_SKIP:
        return "未校验"
    if lv != _V_BAD:
        return "正常"
    why = nt.split("：", 1)[1] if "：" in nt else nt
    return "打不开(%s)" % why.split("（")[0].strip()[:60]


def verify_pair(fname: str, src: bytes, out: bytes, src_name: str) -> tuple:
    """上传前双检：先测原件基线、再测产物。返回 (是否拦下, 日志列文本, 两边是否都能打开)。

    只有一种情况拦：产物判坏而原件不坏——那是我们改坏的，绝不上传。
    原件本来就坏（源件问题）照常上传并记 source_bad：这类连 refix 也修不好（膨胀只追加不改写），
    拦下来只会让整批卡在一个源件毛病上，应当交回源头处理。
    双检口径还能防一类误杀：有些成品只是「原件自带的书签树小瑕」，没有基线就会错算到膨胀头上。
    """
    if _verify_mode == "off":
        return False, "未校验", True
    lv_src, cd_src, nt_src = verify_pdf(src)
    lv_out, cd_out, nt_out = verify_pdf(out)
    blocked = (lv_out == _V_BAD and lv_src != _V_BAD) or (_verify_strict and lv_out == _V_SUSP)
    note = f"源件{_vshort(lv_src, cd_src, nt_src)}/产物{_vshort(lv_out, cd_out, nt_out)}"
    if blocked and lv_out != _V_BAD:
        note += f"（可疑被严格口径拦回：{cd_out}）"
    with _lock:
        _stats["verify_checked"] += 1
        if lv_src == _V_BAD:
            _stats["verify_source_bad"] += 1
        if lv_out == _V_BAD:
            _stats["verify_bad"] += 1
        if blocked:
            _stats["verify_block"] += 1
    return blocked, note, not (lv_src == _V_BAD or lv_out == _V_BAD)


# ---------------- 单文件处理（含并发安全的分类与认领） ----------------

_lock = threading.Lock()
_stats = {
    "processed": 0, "repaired": 0, "refix": 0, "fail": 0,
    "skip_done": 0, "skip_big": 0, "skip_notpdf": 0, "claim_fail": 0,
    "skip_notrefix": 0,
    "plan_new": 0, "plan_repair": 0, "plan_refix": 0,
    "verify_checked": 0, "verify_bad": 0,
    "verify_source_bad": 0, "verify_block": 0,
    "t_dl": 0.0, "t_inf": 0.0, "t_pu_s": 0.0, "t_pu_l": 0.0, "t_ver": 0.0,
    "bytes_dl": 0, "bytes_up": 0, "bytes_added": 0,
}


def log(msg: str):
    with _lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # 极端环境（如 stdout 无法切 UTF-8 且控制台是 GBK）编不出某些符号时降级输出，
            # 不让打印炸掉整个任务
            enc = sys.stdout.encoding or "utf-8"
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)


def bump(key: str, n: int = 1):
    with _lock:
        _stats[key] += n


def handled_count() -> int:
    """已处理完（含失败）的文件个数。只给心跳/进度行当吞吐参照，读不加锁的快照。"""
    s = _stats
    return s["processed"] + s["repaired"] + s["refix"] + s["fail"]


_LOG_PATH = None  # 本地处理流水日志路径（仅 apply 时写；由 --log / CONFIG 设定）

# 本次运行的统一时间戳（只有一份日志，拿它插上文件名；行内另有一个逐条时间）。
# 导入时就固定，所以同一轮里多个写入点不会得到不同后缀。
_RUN_STAMP = datetime.datetime.now().strftime("%Y%m%d%H%M%S")


def set_log_path(path: str):
    global _LOG_PATH
    _LOG_PATH = path


def log_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_filename(path: str, enabled: bool = True) -> str:
    """给日志名插上本次运行时间戳：pdf_processed.log → pdf_processed_20261029145550.log。

    目录与扩展名保持原样（.gz 这类多段后缀也只插在最后一段前）；无扩展名就后接 _时间戳。
    enabled=False 直接原样返回（退回旧的固定名追加行为）。
    """
    if not path or not enabled:
        return path
    root, ext = os.path.splitext(path)
    if not ext or os.path.basename(path).startswith("."):
        return f"{path}_{_RUN_STAMP}"
    return f"{root}_{_RUN_STAMP}{ext}"


def log_done(entry: str):
    """把一次成功处理追加到本地日志（只写文本，不落 PDF；每行关闭前 flush，尽量适配并发追加）。"""
    if not _LOG_PATH:
        return
    line = f"{log_ts()}\t{entry}"
    with _lock:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"警告：写日志失败：{e}", file=sys.stderr)


def log_failed(fname: str, why, status: str = "FAILED") -> None:
    """失败 / 被拦下的文件也落一行（状态列不是 PROCESSED 之类）：拿这份日志当名单（--files-from）
       就能定点重试这批。字段顺序与成功行同形，解析名单时取到的仍是文件名。"""
    if not _LOG_PATH:
        return
    msg = " ".join(str(why).split())[:200]
    line = f"{log_ts()}\t{status}\t{fname}\t-\t-\t{msg}"
    with _lock:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"警告：写日志失败：{e}", file=sys.stderr)


def make_marks():
    """✓/✗ 在部分控制台编码下可能编不出，探测后必要时降级为 [OK]/[X]。"""
    try:
        "✓✗".encode(sys.stdout.encoding or "utf-8")
        return "✓", "✗"
    except UnicodeEncodeError:
        return "[OK]", "[X]"


def _sidecar_keys_legacy(key: str):
    """历史遗留的 sidecar 键（排除现行 _sssss），用于尊重旧后缀、避免二次处理。"""
    root, ext = os.path.splitext(key)
    return [f"{root}{suf}{ext}" for suf in _ARTIFACT_SUFFIXES if suf != "_sssss"]


def _classify(client: Minio, key: str, skey: str, refix: bool = False,
              refix_legacy: bool = False, refix_only: bool = False):
    """(kind, size, artifact)：kind ∈ done/refix/repair/new/big/notrefix。
       - 现行 _sssss 存在→ done（已膨胀）或 repair（已备份但 X.pdf 未覆盖，可补做）
       - 历史 _smallsmall/_s 存在→ 视为 done 跳过，不重跑（artifact 存实际命中的 sidecar）
       - 无 sidecar：按 X.pdf 尺寸判 big 或 new
       - refix=True：把“已完成”改判为 refix，用 sidecar 里的原件按当前规则重做一份
         覆盖 X.pdf——填充方式变更后要重跑已处理的那批文件就用它
       - 历史后缀默认不重做（需 --refix-legacy）：因为无法区分“上一代 sidecar”与
         “恰好叫 X_s.pdf 的另一份真实文档”，拿后者覆盖前者会丢原件
       - refix_only=True：本轮只动「有原件备份」的文件，没有任何 sidecar 的一律不碰
         （默认的 --refix 会顺带把还没处理过的新文件也首次处理掉，不想要那部分就用它）
    """
    size = client.head_size(key)
    if client.exists(skey):
        if size >= _BIG:
            return ("refix" if refix else "done"), size, skey
        return "repair", size, skey
    for alt in _sidecar_keys_legacy(key):
        if client.exists(alt):
            return ("refix" if (refix and refix_legacy) else "done"), size, alt
    if refix_only:
        # 没备份：小于上限的归 notrefix（本轮不碰）；已达上限的仍报 big（语义是无原件可重做）
        return ("notrefix" if size < _BIG else "big"), size, None
    return ("big" if size >= _BIG else "new"), size, None


def process_one(client: Minio, key: str, idx: int, total: int, apply: bool,
                quiet: bool, cols: int, mk_ok: str, mk_fail: str, pre=None,
                refix: bool = False, refix_legacy: bool = False,
                refix_only: bool = False):
    """pre：预筛阶段已得到的 (kind, size, artifact)，复用以避免重复 HEAD。"""
    fname = os.path.basename(key)
    skey = s_key_of(key)
    sname = os.path.basename(skey)
    try:
        kind, size, artifact = (pre if pre is not None
                                else _classify(client, key, skey, refix, refix_legacy,
                                               refix_only))

        # 已完成 / 规则外 / 只重做模式下没备份：直接跳过（不下载）
        if kind == "done":
            bump("skip_done")
            if artifact == skey:
                reason = f"{sname} 存在且已膨胀"
            else:
                reason = f"历史产物 {os.path.basename(artifact)} 存在，不再重跑"
            log(f"[{idx}/{total}] – {fname}：已完成（{reason}），跳过")
            return
        if kind == "notrefix":
            bump("skip_notrefix")
            log(f"[{idx}/{total}] – {fname}：没有 {sname} 备份（{human_size(size)}），"
                f"--refix-only 本轮不碰，跳过")
            return
        if kind == "big":
            bump("skip_big")
            log(f"[{idx}/{total}] – {fname}：原始 {human_size(size)} 超过 {human_size(_BIG)} 且无任何产物 sidecar，规则外跳过")
            return

        # 干跑：只报告计划，不下载/不写（不落盘，本地不留任何 PDF）
        if not apply:
            if kind == "new":
                bump("plan_new")
                log(f"[{idx}/{total}] [dry·新建] {fname}：{human_size(size)} → "
                    f"将建 {sname} 并以约 {human_size(target_for(size))} 覆盖原名下")
            elif kind == "repair":
                bump("plan_repair")
                log(f"[{idx}/{total}] [dry·补做] {fname}：已有 {sname} 但未覆盖，将补做膨胀覆盖")
            else:  # refix
                bump("plan_refix")
                log(f"[{idx}/{total}] [dry·重做] {fname}：从 {os.path.basename(artifact or skey)} 取原件，"
                    f"按当前规则重新膨胀并覆盖 {fname}")
            return

        # —— 实际执行 ——
        if kind in ("repair", "refix"):
            # 原件已备份在 sidecar，需要重做 X.pdf：
            #   repair = X.pdf 还是小文件（上次崩在覆盖前）；refix = X.pdf 是旧规则的膨胀版
            # 两者同样：从 sidecar 取原件 → 按当前规则膨胀 → 覆盖 X.pdf
            src_key = artifact or skey
            src_name = os.path.basename(src_key)
            verb = "补做" if kind == "repair" else "重做"
            prog = Progress(f"{fname} 取副本", 0, quiet, cols, _lock)
            t = time.time()
            data = client.get_bytes(src_key, prog)
            t_dl = time.time() - t
            prog.close()
            if not is_pdf_bytes(data):
                bump("skip_notpdf")
                log(f"[{idx}/{total}] – {fname}：{src_name} 内容非 PDF，跳过")
                return
            if len(data) >= _BIG:
                # sidecar 本身已是个膨胀版（不是原件），再膨一次只会更糟
                bump("skip_big")
                log(f"[{idx}/{total}] – {fname}：{src_name} 已 {human_size(len(data))}，不像原件，跳过{verb}")
                return
            target = target_for(len(data))
            if target is None:
                bump("skip_big")
                return
            t = time.time()
            inflated, _ = inflate_bytes(data, target)
            t_inf = time.time() - t
            # 上传前校验：拿 sidecar 里的原件当基线，只有「原件好好的、我们做的产物坏了」才拦
            t = time.time()
            blocked, vnote, vclean = verify_pair(fname, data, inflated, src_name)
            t_ver = time.time() - t
            if blocked:
                log(f"[{idx}/{total}] {mk_fail} {fname}：{verb}产物不过校验，不覆盖 {fname}（{vnote}）")
                log_failed(fname, f"{verb}产物不过校验：{vnote}", status="BLOCKED")
                return
            prog = Progress(f"{fname} 覆盖", len(inflated), quiet, cols, _lock)
            t = time.time()
            client.put_bytes(key, inflated, prog)
            t_pu = time.time() - t
            prog.close()
            with _lock:
                _stats["repaired" if kind == "repair" else "refix"] += 1
                _stats["t_dl"] += t_dl
                _stats["t_inf"] += t_inf
                _stats["t_ver"] += t_ver
                _stats["t_pu_l"] += t_pu
                _stats["bytes_dl"] += len(data)
                _stats["bytes_up"] += len(inflated)
                _stats["bytes_added"] += max(0, len(inflated) - len(data))
            entry = (f"{'REPAIRED' if kind == 'repair' else 'REFIXED'}\t{fname}\t"
                     f"<- {src_name}(原件副本) + {fname}(膨胀覆盖)\t"
                     f"{human_size(len(data))}->{human_size(len(inflated))}\t"
                     f"取副本 {human_secs(t_dl)} 膨胀 {human_secs(t_inf)} 校验 {human_secs(t_ver)} "
                     f"覆盖 {human_secs(t_pu)}\t校验：{vnote}")
            log_done(entry)
            log(f"[{idx}/{total}] {mk_ok} {fname}：{verb}覆盖 {human_size(len(data))}→"
                f"{human_size(len(inflated))} 膨胀 {human_secs(t_inf)} 校验 {human_secs(t_ver)} "
                f"覆盖 {human_secs(t_pu)}" + ("" if vclean else f"〔{vnote}〕"))
            return

        # kind == "new"
        prog = Progress(f"{fname} 下载", 0, quiet, cols, _lock)
        t = time.time()
        data = client.get_bytes(key, prog)
        t_dl = time.time() - t
        prog.close()
        if not is_pdf_bytes(data):
            bump("skip_notpdf")
            log(f"[{idx}/{total}] – {fname}：非 PDF 内容，跳过")
            return
        target = target_for(len(data))
        if target is None:
            bump("skip_big")
            log(f"[{idx}/{total}] – {fname}：下载后 {human_size(len(data))} 超过 {human_size(_BIG)}，规则外跳过")
            return

        t = time.time()
        inflated, _ = inflate_bytes(data, target)
        t_inf = time.time() - t
        # 上传前校验：放在「原子认领」之前——拦下的时候连 _sssss 也不建，下次重跑仍会重试，
        # 不会留下一个“已完成”的假标记让后续轮次跳过这个坏产物
        t = time.time()
        blocked, vnote, vclean = verify_pair(fname, data, inflated, sname)
        t_ver = time.time() - t
        if blocked:
            log(f"[{idx}/{total}] {mk_fail} {fname}：膨胀产物不过校验，不上传（{vnote}）")
            log_failed(fname, f"膨胀产物不过校验：{vnote}", status="BLOCKED")
            return

        # 原子认领：条件创建 X_sssss.pdf（只有成功者才允许覆盖 X.pdf）
        prog = Progress(f"{fname} 传副本", len(data), quiet, cols, _lock)
        t = time.time()
        claimed = client.put_conditional(skey, data)
        t_pu_s = time.time() - t
        prog.close()
        if not claimed:
            bump("claim_fail")
            log(f"[{idx}/{total}] – {fname}：{sname} 已被他人认领，跳过（不覆盖）")
            return

        # 覆盖 X.pdf 为膨胀版
        prog = Progress(f"{fname} 传扩大", len(inflated), quiet, cols, _lock)
        t = time.time()
        client.put_bytes(key, inflated, prog)
        t_pu_l = time.time() - t
        prog.close()

        with _lock:
            _stats["processed"] += 1
            _stats["t_dl"] += t_dl
            _stats["t_inf"] += t_inf
            _stats["t_ver"] += t_ver
            _stats["t_pu_s"] += t_pu_s
            _stats["t_pu_l"] += t_pu_l
            _stats["bytes_dl"] += len(data)
            _stats["bytes_up"] += len(data) + len(inflated)
            _stats["bytes_added"] += max(0, len(inflated) - len(data))
        entry = (f"PROCESSED\t{fname}\t-> {sname}(原件副本) + {fname}(膨胀版)\t"
                 f"{human_size(len(data))}->{human_size(len(inflated))}\t"
                 f"下载 {human_secs(t_dl)} 膨胀 {human_secs(t_inf)} 校验 {human_secs(t_ver)} "
                 f"传副本 {human_secs(t_pu_s)} 传扩大 {human_secs(t_pu_l)}\t校验：{vnote}")
        log_done(entry)
        log(f"[{idx}/{total}] {mk_ok} {fname}：{human_size(size)}→{human_size(len(inflated))} "
            f"下载 {human_secs(t_dl)} 膨胀 {human_secs(t_inf)} 校验 {human_secs(t_ver)} "
            f"传副本 {human_secs(t_pu_s)} 传扩大 {human_secs(t_pu_l)}"
            + ("" if vclean else f"〔{vnote}〕"))
    except Exception as e:
        bump("fail")
        log(f"[{idx}/{total}] {mk_fail} {fname}：{e}")
        log_failed(fname, e)


# ---------------- 定向名单（CONFIG["FILES"] / --files）：只处理点名的这几个文件 ----------------

_ARTIFACT_RE = re.compile(r"(" + "|".join(re.escape(s) for s in _ARTIFACT_SUFFIXES) + r")$", re.I)


def parse_file_list(raw) -> list:
    """把名单来源拆成条目：支持 list/tuple（CONFIG['FILES']）与逗号或换行分隔的字符串。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = str(raw).replace("\n", ",").split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def entry_candidates(entry: str) -> list:
    """一条名单条目 → 依次尝试的「原件名（不含 .pdf）」列表。

    容错这些写法：X.pdf / X_sssss.pdf（产物名，归一到 X）/ 带本地或桶内路径的完整串 /
    体检报告里的 CSV 行（取其中第一个以 .pdf 结尾的字段）。
    现行后缀 _sssss 一定是我们生成的 sidecar，名字不可能属于原件，直接归一；
    历史后缀 _s/_smallsmall 先按旧产物理解（目标 X.pdf），找不到时再把整个名字
    当成原件名重试一次——避免把真叫 ABC_S.pdf 的文档误归一后拿去覆写另一个文件。
    Windows 下用 PowerShell Out-File / 记事本存出的清单带 UTF-8 BOM 头，不剔掉会把首行
    名字弄成一个根本不存在的文件，因此这里连 BOM 字符一起剔。
    """
    s = str(entry).strip().strip("\"'\ufeff").replace("\\", "/")
    if not s:
        return []
    if "," in s:                       # CSV/表格行：只取带 .pdf 的字段，不当成多个名字
        fields = [p.strip().strip("\"'\ufeff") for p in s.split(",")]
        pdfs = [p for p in fields if p.lower().endswith(".pdf")]
        if not pdfs:
            return []
        s = pdfs[0]
    name = s.split("/")[-1].strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    if not name:
        return []
    m = _ARTIFACT_RE.search(name)
    if not m:
        return [name]
    stem, suf = name[:m.start()], m.group(1).lower()
    if not stem:
        return [name]
    return [stem] if suf == "_sssss" else [stem, name]


def load_files_from(path: str, from_config: bool = False) -> list:
    """从文本清单读名单：每行一个文件名，# 开头/空行忽略；整行带逗号的当作一条（CSV 直接粘）；
       整行带制表符的当作自家流水日志（pdf_processed_*.log 这类 TSV）：取第一个以 .pdf 结尾的字段。
       路径支持通配（pdf_processed_*.log）：日志现在每轮一份带时间戳的新文件，想拿“历史所有轮”
       的累计名单当一个清单用就不用手抄文件名；按修改时间从新到旧展开并报出读到哪几份。
       路径解析：命令行 --files-from 给的按当前工作目录理解（跟其它命令行路径一致）；
       CONFIG['FILES_FROM'] 的默认值按脚本目录落地（Linux 上 nohup/systemd 任意目录都能找到）。"""
    if not path:
        return []
    if os.path.isabs(path):
        p = path
    elif from_config:
        p = os.path.join(_SCRIPT_DIR, path)
    else:
        p = os.path.abspath(path)
    if any(ch in os.path.basename(p) for ch in "*?["):
        # 只让通配作用于文件名部分：目录段里带 [ ] 是常见写法（如中文括号），不应被当成模式
        parts = sorted(glob.glob(p), key=os.path.getmtime, reverse=True)
        if not parts:
            sys.exit(f"错误：名单通配没匹配到任何文件：{p}")
        tail = "…" if len(parts) > 6 else ""
        print(f"名单通配命中 {len(parts)} 份文件（新→旧）："
              + "、".join(os.path.basename(x) for x in parts[:6]) + tail)
        files = parts
    else:
        files = [p]
    lines = []
    for one in files:
        try:
            # utf-8-sig：有 BOM 就吃掉，没有也不影响
            with open(one, encoding="utf-8-sig", errors="replace") as f:
                lines.extend(ln.strip().strip("\ufeff").strip() for ln in f)
        except OSError as e:
            sys.exit(f"错误：读名单文件失败 {one}：{e}")
    out = []
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        if "\t" in ln:
            # 自家流水日志这类 TSV：时间、状态、原件名……只取第一个以 .pdf 结尾的字段（就是原件名）。
            # 后面「-> X_sssss.pdf(原件副本) + X.pdf(膨胀覆盖)」那类附带说明不参与，不然会把产物名
            # 混进名单（虽然也会被归一回同一个文件，但白白多一堆重复条目）。
            cand = [x.strip() for x in ln.split("\t") if x.strip().lower().endswith(".pdf")]
            if not cand:
                continue
            ln = cand[0]
        if "," in ln:
            out.append(ln)
        else:
            out.extend(parse_file_list(ln))
    # 去重并保留原有顺序：多轮流水日志叠加、或同一份名单里写了两次同一个文件，都只会让同一个
    # 对象被 HEAD 好几遍，没意义（4.4 万行名单里这点开销很可观）
    ded, seen = [], set()
    for x in out:
        k = str(x).strip().lower()
        if k and k not in seen:
            seen.add(k)
            ded.append(x)
    if len(ded) != len(out):
        print(f"名单去重：{len(out)} 条 → {len(ded)} 个不同文件（流水日志里同一个文件会被多轮重复记录）")
    return ded


def resolve_files_arg(args) -> list:
    """定向名单取值优先级：--files / --files-from > 环境变量 PDF_FILES > CONFIG。
       --files / --files-from 只写参数不带值（空串）= 显式关掉名单，回到全目录模式。"""
    if getattr(args, "files", None) == "":
        return []
    cli = parse_file_list(getattr(args, "files", None))
    if cli:
        return cli
    if getattr(args, "files_from", None) == "":
        return []
    if getattr(args, "files_from", None):
        return load_files_from(args.files_from)
    env = parse_file_list(os.environ.get("PDF_FILES"))
    if env:
        return env
    names = list(CONFIG.get("FILES") or [])
    src = str(CONFIG.get("FILES_FROM") or "").strip()
    if src:
        names.extend(load_files_from(src, from_config=True))
    return [str(x).strip() for x in names if str(x).strip()]


def head_probe(client: Minio, key: str):
    """HEAD 探单个对象，返回 (取到?, size, 错误说明)。
       404 与 403 都算「按这个名字取不到对象」：MinIO/S3 对不存在的 key 常常回 403（而不是
       404），跟主流程 exists() 把异常当不存在的口径保持一致，否则每个未命中都会被当成故障。
       其它异常（网络/时钟/5xx）如实上报，别让「探测失败」伪装成「文件不存在」。"""
    try:
        return True, client.head_size(key), ""
    except urllib.error.HTTPError as e:
        if e.code in (404, 403):
            return False, 0, ""
        return False, 0, f"HTTP {e.code}"
    except Exception as e:
        return False, 0, str(e)


def lookup_stem(client: Minio, prefix: str, stem: str, refix_only: bool = False):
    """按「原件名 X」定位并判定该怎么处理，返回 (item 或 None, 说明)。
       item = (key, kind, size, artifact)，与 collect_pending 的元素同构：
         有 _sssss 且 X.pdf 已是膨胀版 → refix（按当前规则重做，就是「重新做一遍」）
         有 _sssss 但 X.pdf 还是小的/不见了 → repair（上次败在覆盖前，补做覆盖）
         只有历史后缀 sidecar → refix，并显式指定从那个 sidecar 取原件
           （名单是人手写的，不存在「误把真实文档当旧 sidecar」的风险，所以不需 --refix-legacy）
         只有 X.pdf 且小于上限 → new（按新规则处理；refix_only=True 时不碰）
         只有 X.pdf 且已超上限又没备份 → 拒绝重做（手上没原件，重做只会把膨胀版再膨一次）"""
    key = f"{prefix}/{stem}.pdf"
    skey = s_key_of(key)
    ok_t, size_t, err_t = head_probe(client, key)
    if err_t:
        return None, f"探测 {stem}.pdf 失败：{err_t}"
    ok_s, _, err_s = head_probe(client, skey)
    if err_s:
        return None, f"探测 {stem}_sssss.pdf 失败：{err_s}"
    if ok_s:
        return (key, ("refix" if size_t >= _BIG else "repair"), size_t, skey), ""
    for alt in _sidecar_keys_legacy(key):
        ok_a, _, _ = head_probe(client, alt)
        if ok_a:
            return (key, "refix", size_t, alt), ""
    if not ok_t:
        return None, (f"桶里既没有 {stem}.pdf，也没有 {stem}_sssss.pdf"
                      "（确认名字、目录前缀与读权限；对象名区分大小写）")
    if size_t >= _BIG:
        return None, (f"{stem}.pdf 已 {human_size(size_t)}（超过 {human_size(_BIG)}）且没有任何 _sssss 备份，"
                      "手上没原件可取，重做只会二次膨胀，拒绝处理")
    if refix_only:
        return None, (f"{stem}.pdf（{human_size(size_t)}）没有任何 _sssss 备份，"
                      "--refix-only 只动有备份的，本轮不新建")
    return (key, "new", size_t, None), ""


def resolve_named_targets(client: Minio, prefix: str, entries: list, workers: int = 1,
                          refix_only: bool = False):
    """定向名单 → (pending, misses, dup)。顺序即名单顺序；misses=[(原始条目, 原因)]。"""
    def resolve_one(entry):
        why = "没从这条里解析出文件名"
        for stem in entry_candidates(entry):
            item, why = lookup_stem(client, prefix, stem, refix_only)
            if item:
                return entry, item, ""
        return entry, None, why

    if workers > 1 and len(entries) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            results = list(pool.map(resolve_one, entries))
    else:
        results = [resolve_one(e) for e in entries]

    pending, misses, seen, dup = [], [], set(), 0
    for entry, item, why in results:
        if not item:
            misses.append((entry, why))
            continue
        if item[0] in seen:               # X.pdf 与 X_sssss.pdf 同时写 = 同一个文件
            dup += 1
            continue
        seen.add(item[0])
        pending.append(item)
    return pending, misses, dup


# ---------------- 待处理预筛（并发 HEAD，把配额留给真正没处理过的文件） ----------------

def classify_safe(client: Minio, key: str, refix: bool = False, refix_legacy: bool = False,
                  refix_only: bool = False):
    """_classify 的容错版：探测失败（对象缺失/网络异常）记失败并返回 ('error', 0, None)。"""
    try:
        return _classify(client, key, s_key_of(key), refix, refix_legacy, refix_only)
    except Exception as e:
        bump("fail")
        log(f"– {os.path.basename(key)}：探测失败，跳过（{e}）")
        log_failed(os.path.basename(key), f"探测失败：{e}")
        return "error", 0, None


def collect_pending(client: Minio, keys: list, limit: int, workers: int,
                    quiet: bool, refix: bool = False, refix_legacy: bool = False,
                    refix_only: bool = False) -> list:
    """并发 HEAD 预筛，按目录顺序返回最多 limit 个待处理项 (key, kind, size, artifact)。
    limit<=0 = 全部。已完成/规则外在此就过滤掉（并计入 skip 统计），所以 -n 统计的是
    “真的还没处理”的个数，而不是列表前 N 个。refix=True 时“已完成”也算待处理（重做）；
    refix_only=True 时没有任何 sidecar 的文件不纳入待处理（只重做有备份的，不新建）。"""
    out, window, i = [], max(workers * 8, 32), 0
    hb = Heartbeat("预筛中，已探测", quiet, len(keys))
    probed = 0

    def probe(key):
        return key, classify_safe(client, key, refix, refix_legacy, refix_only)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
        while i < len(keys) and (limit <= 0 or len(out) < limit):
            batch = keys[i:i + window]
            for key, (kind, size, artifact) in pool.map(probe, batch):
                probed += 1
                hb.tick(probed, f"待处理 {len(out)}")
                if kind in ("new", "repair", "refix"):
                    out.append((key, kind, size, artifact))
                    if limit > 0 and len(out) >= limit:
                        break
                elif kind == "done":
                    bump("skip_done")
                elif kind == "big":
                    bump("skip_big")
                elif kind == "notrefix":
                    bump("skip_notrefix")
            i += len(batch)
            if not quiet:
                print(f"预筛中：已探测 {i}/{len(keys)} 个，找到待处理 {len(out)} 个…", end="\r", flush=True)
    if not quiet:
        print(" " * 70, end="\r", flush=True)
    else:
        log(f"  [{human_secs(time.time() - hb.t0)}] 预筛结束：探测 {probed}/{len(keys)}，待处理 {len(out)}")
    return out


# ---------------- 流水线：边扫描边处理（默认调度） ----------------

def process_stream(client: Minio, keys: list, limit: int, workers: int, apply: bool,
                   quiet: bool, cols: int, mk_ok: str, mk_fail: str,
                   refix: bool = False, refix_legacy: bool = False,
                   refix_only: bool = False, scan_workers: int = 0) -> tuple:
    """边 HEAD 边处理：扫到一个待处理项就立刻投给处理线程池，不再等整份目录探完才开工。

    为什么默认走它：几十万对象的目录，光预筛（每个源文件 2 次 HEAD）就要空等几十分钟，
    这段时间机器只做元数据请求、一个字节都没搬，看着像卡住，实际上是白等。
    怎么不失控：
      · 扫描只发 HEAD，很轻，几个线程就能喂饱处理池（默认取 workers//2，至少 2 个）；
      · 处理侧用信号量背压，在途+排队最多 workers*2+scan_workers 个，所以不会把几十万
        个任务一次性排进队列（内存友好、 Ctrl+C 也能很快收尾），扫描因此自动放慢；
      · 心跳里的「约剩多久」于是变成整个任务的预估完成时间（探测速度已等于处理速度）。
    -n/limit 语义不变：凑够 limit 个待处理就停止扫描（已投喂的照旧做完再收尾）。
    行首 [n/m] 的 m 是「提交那一刻已发现的待处理数」，边扫边长，不是最终总数。
    返回 (探测个数, 发现并投喂个数)。
    """
    scan_workers = max(2, scan_workers if scan_workers > 0 else workers // 2)
    inflight = threading.Semaphore(workers * 2 + scan_workers)
    hb = Heartbeat("扫描中，已探测", quiet, len(keys))
    st = {"probed": 0, "found": 0, "ran": 0}

    def probe(key):
        return key, classify_safe(client, key, refix, refix_legacy, refix_only)

    def work(key, idx, total_hint, kind, size, artifact):
        try:
            process_one(client, key, idx, total_hint, apply, quiet, cols, mk_ok, mk_fail,
                        pre=(kind, size, artifact), refix_only=refix_only)
        finally:
            inflight.release()          # 不管成败都要归还额度，否则背压会锁死

    scan_window = scan_workers * 8
    interrupted = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf") as pool, \
            ThreadPoolExecutor(max_workers=scan_workers, thread_name_prefix="scan") as scan:
        try:
            backlog = set()
            fi = 0
            while True:
                # 补满扫描窗口（-n 凑够就不再扫；已投喂的照做完）
                while (len(backlog) < scan_window and fi < len(keys)
                       and (limit <= 0 or st["found"] < limit)):
                    backlog.add(scan.submit(probe, keys[fi]))
                    fi += 1
                if not backlog:
                    break
                done, backlog = wait(backlog, return_when=FIRST_COMPLETED)
                for f in done:
                    # -n 凑够就收：本批剩下的探测结果不再分类（窗口里已发出去的 HEAD 拉不回，
                    #    但绝不能再多投一个任务，否则 -n 就超发了）
                    if limit > 0 and st["found"] >= limit:
                        break
                    key, (kind, size, artifact) = f.result()
                    st["probed"] += 1
                    hb.tick(st["probed"], f"待处理 {st['found']}，已处理完 {handled_count()}")
                    if kind in ("new", "repair", "refix"):
                        st["found"] += 1
                        st["ran"] += 1
                        inflight.acquire()
                        pool.submit(work, key, st["ran"], st["found"], kind, size, artifact)
                    elif kind == "done":
                        bump("skip_done")
                    elif kind == "big":
                        bump("skip_big")
                    elif kind == "notrefix":
                        bump("skip_notrefix")
                if not quiet:
                    print(f"扫描中：已探测 {st['probed']}/{len(keys)}，待处理 {st['found']}，"
                          f"已处理完 {handled_count()} 个…", end="\r", flush=True)
                if limit > 0 and st["found"] >= limit:
                    break            # 停止扫描（已投喂的照旧做完）
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if not quiet:
                print(" " * 70, end="\r", flush=True)
        # with 退出会等两个池收尾（受背压限制，量很小），所以中断提示打在后面
    if interrupted:
        log("\n[中断] 已停止扫描；在途与已排队的文件已做完，以下为这部分统计：")
    tail = (f"／无备份不碰 {_stats['skip_notrefix']}" if _stats["skip_notrefix"] else "")
    log(f"  扫描结束：探测 {st['probed']}/{len(keys)}，投喂 {st['ran']} 个待处理"
        f"（跳过：已完成 {_stats['skip_done']}／超尺寸 {_stats['skip_big']}{tail}）")
    return st["probed"], st["found"]


# ---------------- 线程池收尾（兼容无 cancel_futures 的老 Python） ----------------

def shutdown_pool(pool, block: bool = False):
    """尽量取消未启动的任务；Python < 3.9 无 cancel_futures 参数时降级。
       （形参叫 block 不叫 wait：模块层已从 concurrent.futures 导入了 wait）"""
    try:
        pool.shutdown(wait=block, cancel_futures=True)
    except TypeError:
        pool.shutdown(wait=False)


# ---------------- 汇总 ----------------

_SUMMARY_LOGGED = False


def log_summary(lines) -> None:
    """把汇总页原样追加进流水日志（每行加 # 前缀：日志被 --files-from 当名单复用时会被跳过，
       不会把统计行读成文件名）。窗口一关就看不到的那些数字，这里留一份；每轮只写一次。"""
    global _SUMMARY_LOGGED
    if not _LOG_PATH or _SUMMARY_LOGGED:
        return
    _SUMMARY_LOGGED = True
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write(f"# ===== {log_ts()} 本轮结束，下面是汇总（上面是逐文件 TSV）=====\n")
            for ln in lines:
                f.write("# " + " ".join(str(ln).split()) + "\n")
            f.write("\n")
    except OSError as e:
        print(f"警告：写日志汇总失败：{e}", file=sys.stderr)


def print_summary(apply: bool, secs: float):
    s = _stats
    out = []

    def emit(line: str = ""):
        print(line)
        out.append(line)

    print()
    emit(f"===== 统计【{'APPLY（已改动桶）' if apply else 'DRY-RUN（未改动桶，加 --apply 才执行）'}】=====")
    if apply:
        emit(f"结果：处理 {s['processed']}，补做 {s['repaired']}，重做 {s['refix']}，失败 {s['fail']}，"
             f"跳过 {s['skip_done'] + s['skip_big'] + s['skip_notpdf'] + s['claim_fail'] + s['skip_notrefix']}"
             f"（已完成 {s['skip_done']}／他人认领 {s['claim_fail']}／超尺寸 {s['skip_big']}"
             f"／非PDF {s['skip_notpdf']}／无备份不碰 {s['skip_notrefix']}）")
        emit(f"各步骤累计耗时：下载 {human_secs(s['t_dl'])}，膨胀 {human_secs(s['t_inf'])}，"
             f"上传前校验 {human_secs(s['t_ver'])}，"
             f"传原件(_sssss) {human_secs(s['t_pu_s'])}，传扩大 {human_secs(s['t_pu_l'])}")
        if _verify_mode != "off":
            emit(f"上传前校验：查了 {s['verify_checked']} 组；源件打不开 {s['verify_source_bad']}，"
                 f"产物打不开 {s['verify_bad']}，拦下未上传 {s['verify_block']}，其余都能打开")
            if s["verify_block"]:
                emit(f"注意：{s['verify_block']} 个产物没上传（桶里仍是原样，也没建 _sssss，重跑会重试）——"
                     f"默认口径下拦的都是「产物比原件坏」（膨胀环节的问题），--verify-strict 还会多拦一批可疑件；"
                     f"逐个文件看流水日志每行最后那列「校验：源件…/产物…」")
        emit(f"数据量：下载 {human_size(s['bytes_dl'])}，上传 {human_size(s['bytes_up'])}"
             f"（其中新增填充 {human_size(s['bytes_added'])}）")
        ndone = s["processed"] + s["repaired"] + s["refix"]
        tail = f"，平均 {human_secs(secs / ndone)}/个" if ndone else ""
        emit(f"总用时 {human_secs(secs)}{tail}")
    else:
        emit(f"计划：新建 {s['plan_new']}，补做 {s['plan_repair']}，重做 {s['plan_refix']}，"
             f"跳过 {s['skip_done'] + s['skip_big'] + s['skip_notrefix']}"
             f"（已完成 {s['skip_done']}／超尺寸 {s['skip_big']}／无备份不碰 {s['skip_notrefix']}），"
             f"失败 {s['fail']}")
        emit("（干跑：未下载/膨胀/上传，无步骤耗时；确认无误后加 --apply 执行）")
        if _verify_mode != "off":
            emit("（干跑不下载，所以也没做上传前校验；--apply 时才会逐份校验产物能不能打开）")
    log_summary(out)


# ---------------- 入口 ----------------

class _Formatter(argparse.ArgumentDefaultsHelpFormatter):
    """默认值为 None（含义是「取本文件 CONFIG」）时不刷 (default: None)，
    避免在 Linux 上看 --help 时被无用默认值误导。"""

    def _get_help_string(self, action):
        if action.default is None:
            return action.help
        return super()._get_help_string(action)


def main():
    ensure_utf8_streams()          # 先切 UTF-8，再创建进度条/探测 ✓✗ 是否可用
    parser = argparse.ArgumentParser(
        description="MinIO 目录 PDF 批量：重命名(_sssss)+膨胀+回传原目录"
                    "（连接与目录默认取本文件 CONFIG，默认干跑，--apply 才改动）",
        formatter_class=_Formatter)
    parser.add_argument("--prefix", default=None,
                        help=f"目录前缀（<bucket>/<路径>）；不传则用 CONFIG['PREFIX']"
                             f"（当前：{CONFIG['PREFIX']}）")
    parser.add_argument("--endpoint", default=None,
                        help=f"MinIO 地址；不传则用 CONFIG['MINIO_ENDPOINT']（当前：{CONFIG['MINIO_ENDPOINT']}）")
    parser.add_argument("--access-key", default=None,
                        help="Access Key；不传则用环境变量 MINIO_ACCESS_KEY 或 CONFIG['MINIO_ACCESS_KEY']")
    parser.add_argument("--secret-key", default=None,
                        help="Secret Key；不传则用环境变量 MINIO_SECRET_KEY 或 CONFIG['MINIO_SECRET_KEY']")
    parser.add_argument("--log", default=None, nargs="?", const="",
                        help=f"本地处理流水日志（相对路径按脚本目录解析；每行最后一列就是校验结论，"
                             f"日志只有这一份）；不传则用 CONFIG['LOG_FILE']（当前：{CONFIG['LOG_FILE']}）；"
                             f"只写参数不带值=不写日志")
    parser.add_argument("--log-ts", default=None, choices=("on", "off"),
                        help=f"日志文件名插上本次运行时间戳（每轮一份新文件）；off=沿用固定文件名逐轮追加；"
                             f"不传则用 CONFIG['LOG_TS']（当前：{CONFIG['LOG_TS']}）")
    parser.add_argument("--verify", default=None, choices=("auto", "tail", "off"),
                        help=f"上传前在内存里校验产物能不能被阅读器打开，判坏的不上传；"
                             f"auto=装了 PyMuPDF 就用内核判、没装自动降级为尾部指针判定；"
                             f"tail=强制零依赖口径；off=不校验（老行为）；"
                             f"不传则用 CONFIG['VERIFY']（当前：{CONFIG['VERIFY']}）")
    parser.add_argument("--verify-strict", action="store_true",
                        help="严格口径：连「可疑」（书签树/回退字体这类实测仍能打开的琐碎告警）"
                             "也拦下不上传；默认只拦 startxref/trailer 定位失败那一类")
    parser.add_argument("--max-mb", type=float, default=None,
                        help=f"原件最大可处理尺寸 MiB（超过且无 _sssss 备份=规则外跳过）；"
                             f"不传则用 CONFIG['MAX_MB']（当前：{CONFIG['MAX_MB']}）。"
                             f"只为单文件验证而调大：放宽后已膨胀的完成版会被当原件再膨一次")
    parser.add_argument("-n", "--limit", type=int, default=None,
                        help="本轮处理上限（只计本轮真要动的文件）；全目录模式默认 10，"
                             "定向名单模式默认=名单全做；0=不限")
    parser.add_argument("--files", default=None, nargs="?", const="",
                        help="定向名单（逗号分隔）：配了就只处理这几个文件、不扫全目录；支持 "
                             "X.pdf / X_sssss.pdf / 省略 .pdf；不传则用环境变量 PDF_FILES 或 "
                             f"CONFIG['FILES']（当前 {len(CONFIG['FILES'])} 个）；只写参数不带值=关闭名单")
    parser.add_argument("--files-from", default=None, nargs="?", const="",
                        help="定向名单文本文件（每行一个名字，# 开头为注释；按当前工作目录解析）；"
                             "只写参数不带值=关闭名单")
    parser.add_argument("--list-cap", type=int, default=0,
                        help="列目录最多收集多少个 PDF 就提前停（0=全量翻页；多机分片须各机设成相同值以保证全集一致）")
    parser.add_argument("--scan-first", action="store_true",
                        help="老行为：先用 HEAD 把整份目录预筛完再开始处理（几十万对象时要空等几十分钟）；"
                             "默认已改成边扫边做。干跑想一次看清全集总数时用它")
    parser.add_argument("--scan-workers", type=int, default=0,
                        help="流水线里专责 HEAD 扫描的线程数（0=自动：取处理线程数的一半、至少 2 个）；"
                             "扫描只发 HEAD 很轻，一般不用调大")
    parser.add_argument("--shards", type=int, default=1, help="多机并发总台数 N（按稳定顺序 index%%N 分片）")
    parser.add_argument("--shard", type=int, default=0, help="本机分片编号 0..N-1")
    parser.add_argument("--retries", type=int, default=None,
                        help=f"单请求网络/5xx 重试次数；不传则用 CONFIG['RETRIES']（当前：{CONFIG['RETRIES']}）")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help=f"本机并发线程数（1=串行）；不传则用 CONFIG['WORKERS']（当前：{CONFIG['WORKERS']}）")
    parser.add_argument("--no-cond", action="store_true", help="禁用条件写认领（仅靠 --shard 防重叠）")
    parser.add_argument("--refix", action="store_true",
                        help="重做已完成的文件：从 _sssss 取原件，按当前规则重新膨胀并覆盖 "
                             "X.pdf（_sssss 不动）；用于修复旧填充规则产出的打不开的那批")
    parser.add_argument("--refix-legacy", action="store_true",
                        help="搭配 --refix：连历史后缀(_smallsmall/_s)的产物也重做"
                             "（风险：无法区分旧 sidecar 与恰好叫 X_s.pdf 的真实文档）")
    parser.add_argument("--refix-only", action="store_true",
                        help="只动「有原件备份」的文件：没有任何 _sssss 备份的一律不碰（不新建）。"
                             "默认的 --refix 会顺带把还没处理过的新文件首次处理掉，"
                             "不想扩大范围就加它（也可单用，等于只做补做/修复）")
    parser.add_argument("--apply", action="store_true", help="真正执行下载/膨胀/上传（会改动桶）；缺省为干跑")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默：不显示进度条")
    args = parser.parse_args()

    prefix = resolve_prefix(args)
    endpoint, ak, sk = resolve_endpoint_ak_sk(args)
    retries = int(args.retries if args.retries is not None else CONFIG["RETRIES"])
    workers_req = int(args.workers if args.workers is not None else CONFIG["WORKERS"])
    if workers_req < 1:
        parser.error("-w/--workers 需 >= 1")
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        parser.error(f"--shard 需在 0..{args.shards - 1}（--shards={args.shards}）")

    # 定向名单：配了就叫「只处理这几个」，没配才走全目录流程；-n 在两种模式下的默认不同
    wanted = resolve_files_arg(args)
    limit = args.limit if args.limit is not None else (0 if wanted else 10)
    if limit < 0:
        parser.error("-n/--limit 不能为负数")

    client = Minio(endpoint, ak, sk, retries=retries, no_cond=args.no_cond)
    # 日志名默认插上本次运行时间戳（每轮一份新文件）：这份日志同时是 --files-from 的名单
    # 来源，滚在同一份里会越读越混、分不出哪批是哪一轮做的。
    ts_mode = str(_pick("LOG_TS", args.log_ts, env_name="PDF_LOG_TS") or "on").lower()
    log_ts_on = ts_mode not in ("0", "false", "no", "off")
    # 日志路径：未显式传 --log 时用 CONFIG['LOG_FILE']；相对路径一律落在脚本同目录，
    # 便于 Linux 上 nohup/systemd 任意工作目录运行都能找到同一份流水日志。
    raw_log = str(CONFIG.get("LOG_FILE") or "") if args.log is None else args.log
    raw_log = ts_filename(raw_log, log_ts_on)
    log_path = "" if not raw_log else (
        raw_log if os.path.isabs(raw_log) else os.path.join(_SCRIPT_DIR, raw_log))
    if log_path:
        set_log_path(os.path.abspath(log_path))

    # 上传前校验的配置（命令行 > 环境变量 > CONFIG），详见「上传前校验」一节
    vmode = str(_pick("VERIFY", args.verify, env_name="PDF_VERIFY") or "auto").lower()
    if vmode not in ("auto", "tail", "off"):
        parser.error(f"--verify 只认 auto/tail/off（当前：{vmode}）")
    vstrict = bool(args.verify_strict) or str(_pick("VERIFY_STRICT", None, env_name="PDF_VERIFY_STRICT")).lower() \
        in ("1", "true", "yes", "on")
    set_verify(vmode, vstrict)
    set_big_limit(_pick("MAX_MB", args.max_mb, env_name="PDF_MAX_MB"))

    cols = shutil.get_terminal_size((100, 24)).columns
    mk_ok, mk_fail = make_marks()
    dry_note = "" if args.apply else "（干跑：不会改动桶内容，加 --apply 才执行）"
    log_note = (f"日志={os.path.abspath(log_path)}" + ("（本轮新文件，含校验列）" if log_ts_on else "（固定名追加）")) \
        if log_path else "日志=关闭"
    if vmode == "off":
        verify_note = "上传前校验：已关（--verify off）"
    else:
        verify_note = (f"上传前校验：{verify_desc()}；结论就在日志每行最后一列，"
                       + ("严格：可疑也拦" if vstrict else "只拦「产物比原件坏」那批"))
    if _BIG != 5 * _MIB:
        print(f"注意：原件尺寸上限已放宽到 {human_size(_BIG)}（--max-mb），"
              f"已膨胀过的完成版也会被当原件再膨一次——只建议配名单点名单个文件做验证")
    t_scan = time.time()

    if wanted:
        # 定向名单模式：只处理点名的这几个，不列目录（4 万个对象翻页对点名没有意义），
        # 改成逐个名字 HEAD 定位；--shards/--list-cap 在这种模式下失去意义。
        if args.shards > 1 or args.list_cap > 0:
            print("提示：定向名单模式下 --shards/--shard/--list-cap 被忽略（不扫全目录，名单即范围）")
        if args.refix:
            print("提示：定向名单本身就含「重做」语义，无需 --refix（已忽略）")
        workers = max(1, min(workers_req, len(wanted)))
        print(f"模式：{'APPLY' if args.apply else 'DRY-RUN'}（定向名单 {len(wanted)} 个条目）{dry_note}")
        print(f"连接：{endpoint}（AK={ak[:4]}****，region={REGION}）")
        print(f"目录：{prefix}")
        print("名单语义：X.pdf 与 X_sssss.pdf 算同一个文件；已膨胀过的从 _sssss 取原件按当前规则"
              "重新膨胀并覆盖 X.pdf（_sssss 不动），没处理过的正常处理")
        print(f"PDF 全程内存处理不落盘（无本地残留）；{log_note}")
        print(verify_note)
        if workers > 1 and not args.quiet:
            print("提示：多线程下各文件进度条会交错，可加 -q 只看逐文件结果行")
        print()

        if limit > 0 and len(wanted) > limit:
            # 名单模式先按 -n 截断再去定位：否则 4 万行名单要先把 4 万个名字全 HEAD 一轮才轮得到 -n，
            # 想试跑 5 个也得等半天。未命中的会逐条照实报，要全量定位就 -n 0。
            print(f"提示：名单共 {len(wanted)} 个条目，按 -n {limit} 只定位处理前 {limit} 个"
                  f"（要全量定位就 -n 0）")
            wanted = wanted[:limit]

        pending, misses, dup = resolve_named_targets(client, prefix, wanted, workers,
                                                     args.refix_only)
        if dup:
            print(f"名单里 {dup} 个条目指向同一个文件（如同时写了 X.pdf 与 X_sssss.pdf），已去重")
        for raw, why in misses:
            print(f"{mk_fail} 未命中：{raw} —— {why}")
        if misses:
            print(f"共 {len(misses)} 个条目没定位到，以下只处理定位成功的（名单与桶内名字不一致时"
                  f"最常见原因是名字打错或不在该目录前缀下）\n")
        if not pending:
            print("名单里没有可处理的对象（全部未命中或无原件可重做）")
            if misses:
                print("提示：若所有条目都未命中，先确认 AK 对该目录有读权限、--prefix/CONFIG['PREFIX'] 指的是实际目录")
            return
        if limit > 0 and len(pending) > limit:
            print(f"名单命中 {len(pending)} 个，按 -n {limit} 截断，本轮只处理前 {limit} 个")
            pending = pending[:limit]
        total = len(pending)
        print(f"定位完成：待处理 {total} 个，用时 {human_secs(time.time() - t_scan)}\n", flush=True)
    else:
        # 列目录策略：默认全量翻页。因为已处理文件按名称排在目录前段，截断列表会“只看
        # 到已完成的一批”而找不到新文件；--list-cap 可显式截断（只在确定前段有未处理时用最省时间）。
        cap = args.list_cap if args.list_cap > 0 else 0
        keys = list_pdfs(endpoint, ak, sk, prefix, quiet=args.quiet, max_pdf=cap)

        sources = [k for k in keys if not is_artifact(k)]
        if args.shards > 1:
            sources = [k for i, k in enumerate(sources) if i % args.shards == args.shard]
        if not sources:
            print("没有找到待处理的 PDF（可能都已处理、分片为空或目录为空）")
            return

        workers = max(1, min(workers_req, len(sources)))
        shard_note = f"（分片 {args.shard}/{args.shards}）" if args.shards > 1 else ""
        print(f"模式：{'APPLY' if args.apply else 'DRY-RUN'}{shard_note}{dry_note}")
        print(f"连接：{endpoint}（AK={ak[:4]}****，region={REGION}）")
        print(f"目录：{prefix}")
        print(f"目录共 {len(sources)} 个源文件（已排除产物后缀），"
              + ("先并发预筛完再处理（--scan-first）" if args.scan_first
                 else "边扫描边处理：HEAD 扫到待处理的就立刻开工，不空等预筛"))
        if args.refix:
            print("重做模式（--refix）：已处理过的文件也会被列入待处理，用 sidecar 里的原件重新膨胀覆盖"
                  + ("（含历史后缀）" if args.refix_legacy else "（仅 _sssss，历史后缀需 --refix-legacy）"))
        if args.refix_only:
            print("只重做模式（--refix-only）：没有任何 _sssss 备份的文件本轮不碰，不会新建")
        print(f"PDF 全程内存处理不落盘（无本地残留）；{log_note}")
        print(verify_note)
        if workers > 1 and not args.quiet:
            print("提示：多线程下各文件进度条会交错，可加 -q 只看逐文件结果行")
        print()

        if not args.scan_first:
            # 默认流水线：以前要先用 HEAD 把整份目录探完（几十万对象≈几十分钟）才开工，
            # 这段时间一个字节都没搬；现在扫到一个就投一个，处理池立刻动起来。
            t_all = time.time()
            probed, found = process_stream(
                client, sources, limit, workers, args.apply, args.quiet, cols, mk_ok, mk_fail,
                args.refix, args.refix_legacy, args.refix_only, args.scan_workers)
            if found == 0:
                print(f"探测了 {probed} 个源文件：没有匹配的待处理项"
                      f"（已完成跳过 {_stats['skip_done']}／超尺寸跳过 {_stats['skip_big']}）")
            print_summary(args.apply, time.time() - t_all)
            if _stats["fail"]:
                sys.exit(2)
            return

        pending = collect_pending(client, sources, limit, workers, args.quiet,
                                  args.refix, args.refix_legacy, args.refix_only)
        # 只在真用了 --refix-only 时才多报这一项，默认输出的节奏不变
        notrix = (f"／无备份不碰 {_stats['skip_notrefix']}"
                  if _stats["skip_notrefix"] else "")
        if not pending:
            print(f"没有需要处理的 PDF（共探测 {len(sources)} 个：已完成跳过 {_stats['skip_done']}"
                  f"／超尺寸跳过 {_stats['skip_big']}{notrix}／失败 {_stats['fail']}）")
            return
        total = len(pending)
        print(f"预筛完成：待处理 {total} 个（已完成跳过 {_stats['skip_done']}，超尺寸跳过 {_stats['skip_big']}"
              + (f"，无备份不碰 {_stats['skip_notrefix']}" if _stats["skip_notrefix"] else "") + "），"
              f"用时 {human_secs(time.time() - t_scan)}\n", flush=True)

    t_all = time.time()

    def run_one(item):
        idx, (key, kind, size, artifact) = item
        process_one(client, key, idx, total, args.apply, args.quiet, cols, mk_ok, mk_fail,
                    pre=(kind, size, artifact), refix_only=args.refix_only)

    items = list(enumerate(pending, 1))
    try:
        if workers > 1:
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf")
            try:
                list(pool.map(run_one, items))
            except KeyboardInterrupt:
                # 不再排新任务，已在途的文件处理完（或请求超时）后退出
                shutdown_pool(pool)
                raise
            finally:
                shutdown_pool(pool)
        else:
            for item in items:
                run_one(item)
    except KeyboardInterrupt:
        log("\n[中断] 已停止；以下为已完成部分的统计：")
    finally:
        print_summary(args.apply, time.time() - t_all)

    if _stats["fail"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
