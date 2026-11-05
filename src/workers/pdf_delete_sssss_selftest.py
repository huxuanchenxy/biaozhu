#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_delete_sssss_selftest.py —— pdf_delete_sssss.py「点名强删」那条路的离线自测

只测纯函数与本目录里已有的名单文件，不发任何网络请求、不动桶，随时可重跑。
为什么值得留这份文件：强删是唯一「绕过校验」的入口，它最容易犯的错是名字口径混用 ——
本脚本的 quarantine 名单里写的是**成品名 X.pdf**，而要删的对象是 **X_sssss.pdf**。
一旦哪天有人把两者混着匹配，删掉的就是语料本体，而桶开不了版本控制，没有反悔机会。
所以这里把「删除目标必须以备份件后缀结尾」当成硬断言钉住，而不只是测命中数。

用法：python pdf_delete_sssss_selftest.py     （退出码 0 = 全过）
兼容性：Python 3.6+，零第三方依赖
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_delete_sssss as D

SUF = ("_sssss",)
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "delete_logs",
                       "6.1_建筑工程专用构件多属性BIM模型数据集_成品语料")

ok = 0
bad = 0


def chk(name, cond, got=""):
    global ok, bad
    if cond:
        ok += 1
        print("PASS %s" % name)
    else:
        bad += 1
        print("FAIL %s  →  %r" % (name, got))


def cand(prod, side=None):
    """造一条候选元组，形状与主流程一致：(备份件key 备份件size 备份件ETag 成品key 成品size 成品ETag)。"""
    d = os.path.dirname(prod)
    base = os.path.basename(prod)
    sidecar = side if side else os.path.join(d, base[:-4] + "_sssss.pdf")
    return (sidecar, 293413, "e1", prod, 8945155, "e2")


# ---------------- 一、canon_pdf_name：只往「成品名」方向归一 ----------------
cases = [
    ("ISO 12543-3-2011.pdf", "iso 12543-3-2011.pdf"),                 # 成品名原样
    ("ISO 12543-3-2011_sssss.pdf", "iso 12543-3-2011.pdf"),           # 备份件名剥后缀
    ("自筹/6_标准件模型库/x.pdf", "x.pdf"),                            # 带目录取 basename
    ("drivdernet_abc/自筹/x_sssss.pdf", "x.pdf"),                      # 带桶名也认
    (u"AP210A_sssss.PDF", "ap210a.pdf"),                               # 大小写混写
    ("a\\b\\c_sssss.pdf", "c.pdf"),                                    # Windows 反斜杠
    ("\"x.pdf\"", "x.pdf"),                                            # 带引号
    ("x", "x"),                                                         # 省略 .pdf：不猜
    ("_sssss.pdf", "_sssss.pdf"),                                       # 名就是后缀本身：不剥成空
]
for src, exp in cases:
    got = D.canon_pdf_name(src, SUF)
    chk("canon %r → %r" % (os.path.basename(src), exp), got == exp, got)

chk("canon 空输入得空串", D.canon_pdf_name("  ", SUF) == "", D.canon_pdf_name("  ", SUF))
chk("canon 只认配置里的后缀（_s 不在白名单就不剥）",
    D.canon_pdf_name("x_s.pdf", SUF) == "x_s.pdf", D.canon_pdf_name("x_s.pdf", SUF))
chk("canon 多后缀配置也认",
    D.canon_pdf_name("x_s.pdf", ("_sssss", "_s")) == "x.pdf",
    D.canon_pdf_name("x_s.pdf", ("_sssss", "_s")))

# ---------------- 二、load_force_names：三种名单形式 + 边界 ----------------
qfile = os.path.join(LOG_DIR, "quarantine_20261104155838.txt")
tfile = os.path.join(LOG_DIR, "to_delete_20261104155838.tsv")
if os.path.isfile(qfile):
    got = D.load_force_names(qfile, SUF)
    chk("真 quarantine 读出 6 个名字", len(got) == 6, sorted(got))
    chk("quarantine 名单已归一成成品名（不带 _sssss）",
        all(("_sssss" not in x) for x in got), sorted(got)[:2])
    chk("quarantine 的 # 表头没被当名字",
        all(x.endswith(".pdf") and "→" not in x for x in got), sorted(got)[:2])
    chk("quarantine 含中文说明没漏进名单",
        all("（" not in x and "TAIL" not in x.upper() for x in got), sorted(got)[:2])
else:
    print("SKIP 找不到 %s（先跑一次 pdf_delete_sssss.py 干跑生成名单）" % qfile)

if os.path.isfile(tfile):
    got2 = D.load_force_names(tfile, SUF)
    chk("真 to_delete.tsv 读出 273 个成品名", len(got2) == 273, len(got2))
    chk("to_delete 里的备份件名已归一（一个 _sssss 都不剩）",
        all(("_sssss" not in x) for x in got2), sorted(got2)[:2])

fd, tmppath = tempfile.mkstemp(suffix=".txt")
os.close(fd)
try:
    with io.open(tmppath, "w", encoding="utf-8", newline="\n") as f:
        f.write(u"\n".join([
            u"# 表头一行 → CODE（也不算名字）",
            u"",
            u"  ",
            u"A.pdf",
            u"A_sssss.pdf",
            u"自筹/B_2/b_sssss.pdf",
            u"施工流程\u2192示意图.pdf",
            u"带空格 的名字_sssss.pdf",
        ]) + u"\n")
    got3 = D.load_force_names(tmppath, SUF)
    exp3 = {u"a.pdf", u"b.pdf", u"施工流程\u2192示意图.pdf", u"带空格 的名字.pdf"}
    chk("手写混写名单：同一文件两种写法只算一个", got3 == exp3, sorted(got3))
    chk("文件名自带箭头不被误切（名字里没空格也没大写码）",
        u"施工流程\u2192示意图.pdf" in got3, sorted(got3))
finally:
    os.remove(tmppath)

# ---------------- 三、pick_forced：删除目标永远是备份件，不是成品 ----------------
keep = [cand(u"自筹/6.1/x1.pdf"), cand(u"自筹/6.1/x2.pdf")]
susp = []
quar = [cand(u"自筹/6.1/ISO 12543-3-2011.pdf"),
        cand(u"自筹/6.1/ISO 23387-2020.pdf"),
        cand(u"自筹/6.1/别的.pdf")]

want = D.canon_pdf_name(u"ISO 12543-3-2011.pdf", SUF)  # quarantine 的写法：成品名
forced, miss = D.pick_forced({want}, keep, susp, quar, SUF)
chk("按成品名点名 → 命中 1 个隔离件", len(forced) == 1, [f[0] for f in forced])
chk("命中项的删除目标是 _sssss 备份件",
    forced and forced[0][0].endswith("_sssss.pdf"), forced and forced[0][0])
chk("删除目标不是成品本体（it[3] 绝不进删除列表）",
    forced and forced[0][0] != forced[0][3], forced and (forced[0][0], forced[0][3]))
chk("名单没写错时未命中为空", miss == [], miss)

# 备份件名写法也应命中同一个
forced_b, miss_b = D.pick_forced({D.canon_pdf_name(u"ISO 23387-2020_sssss.pdf", SUF)},
                                 keep, susp, quar, SUF)
chk("按备份件名点名 → 也命中同一个隔离件",
    len(forced_b) == 1 and forced_b[0][3] == u"自筹/6.1/ISO 23387-2020.pdf",
    [f[3] for f in forced_b])

# keep 里的名字算命中，不算未命中
forced_k, miss_k = D.pick_forced({u"x1.pdf"}, keep, susp, quar, SUF)
chk("点名点到已通过校验的件：不算未命中、也不重复放行",
    forced_k == [] and miss_k == [], (forced_k, miss_k))

# 名单来自别的目录 → 全算未命中（--apply 时主流程会据此拒绝）
forced_m, miss_m = D.pick_forced({u"根本没这个.pdf", u"x2.pdf"}, keep, susp, quar, SUF)
chk("对不上的名单逐项进未命中（供 --apply 拒绝用）",
    forced_m == [] and miss_m == [u"根本没这个.pdf"], (forced_m, miss_m))

# 大小写与路径差异不影响命中
forced_c, _ = D.pick_forced({u"ISO 12543-3-2011_sssss.PDF"}, keep, susp, quar, SUF)
chk("大小写/后缀写法差异仍命中", len(forced_c) == 1, [f[0] for f in forced_c])

# 真名单接进 pick_forced：把 6 个隔离件当 quar，应当全命中且删除目标全是 _sssss
if os.path.isfile(qfile):
    real = D.load_force_names(qfile, SUF)
    fake = [cand(u"自筹/6.1/" + os.path.basename(x)) for x in sorted(real)]
    f2, m2 = D.pick_forced(real, keep, susp, fake, SUF)
    chk("真 quarantine 的 6 个名字全命中", len(f2) == 6 and m2 == [], (len(f2), m2))
    chk("真名单放行的 6 项，删除目标全是 _sssss.pdf",
        all(x[0].endswith("_sssss.pdf") for x in f2), [x[0] for x in f2][:2])

# ---------------- 六、预览后成品被人换过：partner_drift ----------------
# 真删前除了「成品还在吗」，还要核它仍是预览时那一份。判据是纯函数，这里钉口径：
# 方向上只允许“宁可误停不可误放”。
P_SIZE, P_TAG = 999999, "d41d8cd98f00b204e9800998ecf8427e"
chk("成品一模一样 → 没变（不拦删除）",
    D.partner_drift(P_SIZE, P_TAG, P_SIZE, P_TAG) is False,
    D.partner_drift(P_SIZE, P_TAG, P_SIZE, P_TAG))
chk("成品体积变了 → 算被人动过（不删）",
    D.partner_drift(P_SIZE, P_TAG, P_SIZE + 1, P_TAG) is True)
chk("体积同、ETag 变了 → 算被人动过（不删）",
    D.partner_drift(P_SIZE, P_TAG, P_SIZE, "06792e8e16c151cc4c0554b57e8426f1") is True)
chk("S3 回的是字符串体积：同数值不算变",
    D.partner_drift(P_SIZE, P_TAG, str(P_SIZE), P_TAG) is False)
chk("ETag 一边带引号一边不带、内容相同 → 不算变",
    D.partner_drift(P_SIZE, '"%s"' % P_TAG, P_SIZE, P_TAG) is False)
chk("ETag 有一边拿不到 → 不拿它误判，只看体积",
    D.partner_drift(P_SIZE, "", P_SIZE, None) is False)
chk("但 ETag 缺位时体积一变仍拦下",
    D.partner_drift(P_SIZE, "", P_SIZE - 5, None) is True)
chk("体积是垃圾值读不成数 → 当作变了（拿不准就不删）",
    D.partner_drift("abc", P_TAG, P_SIZE, P_TAG) is True)
chk("体积 None → 当作变了", D.partner_drift(None, P_TAG, P_SIZE, P_TAG) is True)
chk("ETag 大小写不一致 → 严格口径算变了（宁可多拦不误放）",
    D.partner_drift(P_SIZE, P_TAG.upper(), P_SIZE, P_TAG) is True)
chk("报错里的 ETag 缩成前 8 位，不涌整串哈希",
    D.etag_short(P_TAG).endswith("…") and len(D.etag_short(P_TAG)) == 9,
    D.etag_short(P_TAG))
chk("etag_short 空值印「无」而不是空串", D.etag_short("") == "无", D.etag_short(""))

print("\n――― 七、本地留底（--backup-local）：消毒落点与比对口径 ―――")
import shutil
import tempfile
import hashlib

MD5_ABC = hashlib.md5(b"abc").hexdigest()
chk("单次 PUT 的 ETag（32 位十六进制）判为 MD5 形态",
    D.etag_is_plain_md5(MD5_ABC) is True)
chk("带引号的 ETag 也算 MD5 形态", D.etag_is_plain_md5('"%s"' % MD5_ABC) is True)
chk("分片上传的「hash-2」不算 MD5 形态（不能直接比摘要）",
    D.etag_is_plain_md5(MD5_ABC + "-2") is False)
chk("空 ETag 不算 MD5 形态", D.etag_is_plain_md5("") is False and D.etag_is_plain_md5(None) is False)
chk("落点保留整个 key 的层级（不同目录的同名件不会互相盖）",
    D.local_path_for("R", "b/x/1.pdf") == os.path.join("R", "b", "x", "1.pdf"),
    D.local_path_for("R", "b/x/1.pdf"))
_s = D._safe_segment("a:b?c*d<e>f|g\\h/i.pdf")
chk("名字里的非法字符全被消毒（Windows 写不出去的那些）",
    not any(c in _s for c in ':*<>?|\\/'), _s)
chk("非法字符只换不删，可读名还在", "abc" in _s.replace("_", "") or "_" in _s, _s)
_long1 = D.local_path_for("R", "p/" + "甲" * 200 + ".pdf")
_long2 = D.local_path_for("R", "p/" + "乙" * 200 + ".pdf")
chk("超长名截短（整段不超 200）", len(os.path.basename(_long1)) < 200, len(os.path.basename(_long1)))
chk("两个不同的超长名不会撞同一个落点", _long1 != _long2)

_tmp = tempfile.mkdtemp(prefix="bl_selftest_")
try:
    _p = os.path.join(_tmp, "a.pdf")
    with open(_p, "wb") as _f:
        _f.write(b"abc")
    _ok, _sz, _md5, _why = D.check_backup(_p, 3, MD5_ABC)
    chk("已存在的本地件：体积与 MD5 都对 → 判可用（不重下）", _ok is True and _sz == 3, _why)
    chk("MD5 算对了", _md5 == MD5_ABC, _md5)
    chk("体积对不上 → 判不可用", D.check_backup(_p, 4, MD5_ABC)[0] is False)
    chk("体积对、MD5 对不上 → 判不可用（内容被换过）",
        D.check_backup(_p, 3, "0" * 32)[0] is False)
    chk("ETag 是分片形态时只核体积，不因比不了摘要就误判",
        D.check_backup(_p, 3, MD5_ABC + "-2")[0] is True)
    chk("文件不存在 → 判不可用", D.check_backup(os.path.join(_tmp, "no.pdf"), 3, MD5_ABC)[0] is False)
    chk("体积拿不准（None）时不因它误判，仍比 MD5",
        D.check_backup(_p, None, MD5_ABC)[0] is True)
finally:
    shutil.rmtree(_tmp, True)

print("\n通过 %d 条，未通过 %d 条" % (ok, bad))
if not os.path.isdir(LOG_DIR):
    print("提示：delete_logs 里没有 6.1 的名单文件，上面与真名单相关的断言已跳过")
sys.exit(1 if bad else 0)
