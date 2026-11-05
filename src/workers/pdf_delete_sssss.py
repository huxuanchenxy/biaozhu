#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_delete_sssss.py —— 删掉「已膨胀成功、且成品可用」的 X_sssss.pdf 备份件

为什么需要它：X_ssssss.pdf 是原件的唯一凭据，膨胀版 X.pdf 一旦确认能用，原件备份件就可以从
桶里清走。但你们的桶开不了版本控制，删除是永久的 —— 所以本脚本的核心不是「删」，而是
「删之前先把原件复制进同桶的回收站前缀，核对复制成功再删」，让误删还能从桶里捞回来。

流程（五阶段；不带 --apply 时一个写请求都不发）：
  [1/5] 列目录（只列这一层，边扫边报进度）
  [2/5] 按配对关系分四类：可删候选 / 同层无膨胀版 / 膨胀版体积不足 / 与本次无关
  [3/5] 核验膨胀版 X.pdf 到底能不能用（三档，见 --verify）
  [4/5] 预览报告 + 名单落盘，等你输 yes
  [5/5] 执行：COPY 进回收站 → HEAD 核对回收站副本 → DELETE 原件 → HEAD 确认已消失

回滚：--restore 把回收站里的原件复制回原位（默认不删回收站副本，捞干净了再让运维清）

为什么不必下载整份文件就能判「可以删」：
  ListObjectsV2 每行本来就带 Size 与 ETag，「同层有没有 X.pdf、多大」是列目录的免费副产品；
  「X.pdf 尾部结构好不好」用 HTTP Range 只取尾窗（默认 24KB）就够。膨胀是纯追加不改写，
  头部 %PDF- 魔数等于原件原样，不必再取。真正需要整份字节的是「阅读器一定打得开」这种强
  结论，那由 --verify full 负责（整份读进内存喂 PyMuPDF，不写磁盘），代价是每份 8~11MB 流量。

三档校验（越往下越慢越准；默认 tail）：
  size  只看体积达标                    0 个额外请求/份   —— 最粗，摸底时用
  tail  Range 取尾窗验 %%EOF 与填充结束标记，并对 startxref 指针验真   1 请求/份 —— 默认
  full  整份读进内存用 PyMuPDF 真开一遍  约 10MB/份        —— 最准，适合抽验定标；没装则自动降级 tail

安全边界（本脚本会真删对象，下面每一条都是拦着用的）：
  - 默认 DRY-RUN：不带 --apply 绝不动桶
  - 回收站优先：COPY 失败或副本核对不上 → 该件一律不删；--no-trash 要额外确认才能跳过
  - 本地留底优先（--backup-local）：原件先 GET 到本地、比过体积与 MD5 才删，留底失败那一件就不删；
    桶不让你 PutObject（建不了回收站、--restore 也用不了）时，这是唯一走得通的底
  - 白名单：只删「<名字>_sssss.pdf」形态的 key；膨胀版本体、.md、.json、历史后缀都不进候选
  - 后缀太短（如 _s）直接拒绝：无法与「恰好叫 X_s.pdf 的真实文档」区分，删除不接受这种歧义
  - 只列一层（delimiter='/'）：子目录里的东西一律不碰，宁可不删
  - 用 --max-list 截断过列目录 → 拒绝 --apply：没看到的 X.pdf 可能只是没列到
  - 回收站前缀本身排除在候选之外，避免把自己复制进去的件再删一遍
  - 体积门槛：膨胀版 < --min-inflated（默认 5MiB，与处理脚本 _BIG 同口径）判「疑似半成品」不删
  - 校验拿不准（可疑/异常）一律不删，进隔离清单，绝不猜
  - 点名强删（--force-from）只在三个硬条件下放行：只从本轮已扫出的候选里按名字挑（绝不按名单拼 key 去删）、
    删的仍是 <名字>_sssss.pdf 备份件、照旧先复制进回收站再删；名单里有任何一个名字对不上候选，
    就拒绝 --apply（对不上通常是 --prefix 写错或名单来自别的目录，这种情况不能照剩下的删）
  - 删除前 HEAD 复核大小与 ETag：预览之后**原件备份**被人动过就停手
  - 删除前再核一次**膨胀版本体**：不在了、或体积/ETag 与预览时不符（被人换过一份）都不删
    —— 强删件尤其靠这条：它唯一的兜底就是「成品仍是我预览时看过的那一份」
  - 连续失败达 --stop-on-error（默认 5）立刻中止，防止凭据或网关异常时乱删
  - Range 请求若被网关回 200（不认区间读）立刻中止：绝不悄悄退化成整份下载打爆流量
  - 非交互终端必须显式 --yes，否则拒绝执行
  - 中断可续（已删的记为「已不存在」）
  - 边做边记：开跑前就建好流水日志与成功清单并写表头，每件一完成立刻追加一行并 flush（行内含完成时刻
    与单件耗时）；失败清单首个失败即落盘。删除不可逆，中途断网/断电/被杀时「已删了哪些」已经在盘上，
    不靠跑完才出的文件。末尾再补一段汇总（删前/删后计数与释放量）

用法示例：
  # ① 小目录先跑通（干跑，只出预览与名单）
  python pdf_delete_sssss.py --endpoint http://10.89.14.234:12001 --prefix "桶/目录/成品语料"

  # ② 抽 200 个做强校验，给 tail 档定标
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --verify full --sample 200

  # ③ 对上之后真删（先复制进回收站再删，预览后要输 yes）
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --apply

  # ④ 发现删错了，从回收站捞回原位
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --restore --apply

  # ⑤ 大目录只数数量、不校验不删
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --verify size --max-list 200000

  # ⑥ 隔离件不打算修了，点名强删（名单直接吃本脚本自己的 quarantine_*.txt / to_delete_*.tsv）
  #   先不带 --apply 干跑，看「名单命中几个」对不对数；对上了再加 --apply
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --force-from "delete_logs/.../quarantine_xxx.txt"
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --force-from ".../quarantine_xxx.txt" --apply

  # ⑦ 往桶里写不进去（PutObject 被拒 → 建不了回收站、--restore 也用不了）：删前先落本地底
  #   开了它就自动跳过桶内 COPY；每件比过体积与 MD5 才删，另存 backup_manifest_*.tsv 记底
  python pdf_delete_sssss.py --endpoint ... --prefix "..." --backup-local "D:/备份/sssss留底" --apply

  凭据：--access-key/--secret-key 或环境变量 MINIO_ACCESS_KEY/MINIO_SECRET_KEY 或同目录 .env
  兼容性：Python 3.6+，零第三方依赖（PyMuPDF 仅在 --verify full 时需要）；Windows/Linux 皆可
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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
except ImportError:
    ThreadPoolExecutor = None

# ---- 配置（改这里就行，不用带一堆参数）----
CONFIG = {
    "ENDPOINT": "",              # 留空：必须 --endpoint 或环境变量 MINIO_ENDPOINT
    "SUFFIX": "_sssss",          # 只认现行产物后缀；历史后缀不在删除白名单里（宁可留着也不误删）
    "MIN_INFLATED_MIB": 5,       # 膨胀版体积分界：低于它判「疑似半成品」，不删（与处理脚本 _BIG 对齐）
    "TAIL_WIN": 24576,           # tail 档读尾部多少字节：验 startxref / %%EOF / 填充标记
    "TRASH_DIR": "_trash_sssss",  # 回收站放本目录下这一层里，和成品同前缀，账号权限一定够用
    "JOBS": 4,                   # 删除/复制并发：别开太大，写操作出问题来不及刹
    "RETRIES": 4,
    "TIMEOUT": 60,
    "STOP_ON_ERROR": 5,
}

_V_OK, _V_SUSP, _V_BAD = "ok", "susp", "bad"
_TAIL_XREF_RE = re.compile(rb"startxref\s+(\d+)\s+%%EOF\s*$")
_TAIL_XREF_ANY_RE = re.compile(rb"startxref\s+(\d+)")
_PAD_END = b"%inflate-end"            # 填充块结束标记（紧邻最后的 %%EOF，与 pdf_check_damaged 同口径）
# 注：不要改用填充块起始标记 %inflate-begin( 做判据——它在几 MB 填充体的开头，尾窗里
# 永远取不到，而且本管线新版 write_padding 根本不写起始标记，会把所有成品判为可疑。
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_HB = {"last": 0.0}
_HEARTBEAT = float(os.environ.get("PDF_HEARTBEAT", "5"))
_lock = threading.Lock()
_run = {"ok": 0, "fail": 0, "skip": 0, "bytes": 0, "consec": 0}
_abort = threading.Event()


def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return ("%d%s" % (num, unit)) if unit == "B" else ("%.1f%s" % (num, unit))
        num /= 1024.0


def human_secs(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%ds" % (sec // 60, sec % 60)
    return "%dh%dm" % (sec // 3600, (sec % 3600) // 60)


def ensure_utf8_streams():
    """CMD/PowerShell 默认码元不是 UTF-8 时会把中文打成乱码，这里尽量原地改成 UTF-8。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None or not hasattr(s, "buffer"):
            continue
        if (getattr(s, "encoding", "") or "").lower() in ("utf-8", "utf8"):
            continue
        try:
            setattr(sys, name, io.TextIOWrapper(s.buffer, encoding="utf-8",
                                                errors="replace", line_buffering=True))
        except Exception:
            pass


def load_env_file(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    for line in io.open(path, encoding="utf-8-sig", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _dotenv():
    """凭据不往本脚本里抄一份：从脚本目录往上逐级找 .env（项目根那份就有 MINIO_ACCESS_KEY/SECRET_KEY）。
       就近优先：越靠外层的先读，越靠脚本的内层覆盖它。"""
    d = os.path.dirname(os.path.abspath(__file__))
    chain = []
    for _ in range(5):
        chain.append(os.path.join(d, ".env"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    vals = {}
    for p in reversed(chain):
        vals.update(load_env_file(p))
    return vals


def _pick(cli, env_names, dotenv_keys, dotenv):
    if cli:
        return str(cli).strip()
    for n in env_names:
        if os.environ.get(n):
            return os.environ[n].strip()
    for k in dotenv_keys:
        if dotenv.get(k):
            return dotenv[k].strip()
    return ""


def _err_code(body):
    """把 S3 错误 XML 抽成一行短码，供日志与提示用。"""
    text = (body or b"").decode("utf-8", "replace")
    m = re.search(r"<Code>(.*?)</Code>", text, re.S)
    if not m:
        return re.sub(r"\s+", " ", text)[:160] or "空响应"
    g = re.search(r"<Message>(.*?)</Message>", text, re.S)
    return "%s：%s" % (m.group(1).strip(), (g.group(1).strip() if g else "未给出"))


def skew_offset(err_body, headers):
    """服务端认为我们时钟偏、回 RequestTimeTooSkewed 时，算出该补的秒差。"""
    text = (err_body or b"").decode("utf-8", "replace")
    if "RequestTimeTooSkewed" not in text and "InvalidDate" not in text:
        return None
    server = headers.get("Date") or headers.get("date")
    if not server:
        return None
    try:
        dt = datetime.datetime.strptime(server, "%a, %d %b %Y %H:%M:%S GMT")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int((dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except Exception:
        return None


# ---------------- SigV4：内存里 key 一律不含桶名，发请求时才补进 path-style 路径 ----------------

# S3 的转义规则和 urllib 默认 safe 字符集不一样：+ = / 等都得转义。
# 这里曾经栽过大跟头：路径与 query 共用过一个宽 safe 集（safe="!$&'()*+,;=:@-._~"），
# 于是 continuation-token 里的 + 和末尾的 = 原样透传，服务端把 query 里的 + 解成空格
# → token 失效 → MinIO 不报错、直接回第一页 → 同一页被扫几百遍，候选名单灌满重复行，
# 看着像「扫完了」其实往后一步都没走。改回与 pdf_backup_sssss.py 同口径（那边翻过
# 19,697 页实证无误）。
_SAFE_SEG = "-_.~"


def _strict_seg(seg):
    return urllib.parse.quote(seg, safe=_SAFE_SEG)


def strict_encode_path(p):
    return "/".join(_strict_seg(x) for x in p.split("/"))


def _canonical_query(params):
    items = sorted((str(k), str(v or "")) for k, v in (params or {}).items())
    return "&".join("%s=%s" % (_strict_seg(k), _strict_seg(v)) for k, v in items)


class S3(object):
    """本脚本要用的动作：HEAD / GET（可 Range）/ LIST / COPY / DELETE。"""

    def __init__(self, endpoint, ak, sk, bucket, retries=4):
        self.endpoint = endpoint.rstrip("/")
        self.ak, self.sk = ak, sk
        self.bucket = str(bucket).strip("/")
        self.retries = max(1, int(retries))
        self.offset = 0
        self.host = urllib.parse.urlparse(self.endpoint).netloc

    def _sign(self, method, pathname, query, payload_hash, now, signed_extra=None):
        amz = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        heads = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz}
        if signed_extra:
            heads.update(signed_extra)
        names = sorted(heads)                       # canonical headers 必须按名字排序
        can_headers = "".join("%s:%s\n" % (n, heads[n]) for n in names)
        ch = ";".join(names)
        creq = "\n".join([method, pathname, query, can_headers, ch, payload_hash])
        scope = "%s/%s/%s/aws4_request" % (date, "us-east-1", "s3")
        sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])

        def mac(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()

        s = mac(mac(mac(mac(("AWS4" + self.sk).encode(), date), "us-east-1"), "s3"), "aws4_request")
        out = {"Host": self.host, "User-Agent": "pdf-delete-sssss/1.0",
               "x-amz-date": amz, "x-amz-content-sha256": payload_hash,
               "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                % (self.ak, scope, ch,
                                   hmac.new(s, sts.encode(), hashlib.sha256).hexdigest())}
        if signed_extra:
            out.update(signed_extra)                # 参与签名的头，实际发送的字节必须一模一样
        return out

    def _send(self, method, key, params=None, rng=None, sign_extra=None):
        pathname = strict_encode_path("/" + self.bucket + "/" + str(key).lstrip("/"))
        query = _canonical_query(params or {})
        now = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(seconds=self.offset))
        headers = self._sign(method, pathname, query, _EMPTY_SHA256, now, sign_extra)
        if rng:
            headers["Range"] = rng                  # Range 不进签名：SigV4 允许带未签的额外请求头
        url = self.endpoint + pathname + (("?" + query) if query else "")
        req = urllib.request.Request(url, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=CONFIG["TIMEOUT"])

    def _call(self, method, key, params=None, rng=None, sign_extra=None):
        for attempt in range(self.retries):
            try:
                return self._send(method, key, params, rng, sign_extra)
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                off = skew_offset(body, e.headers)
                if e.code in (403, 500) and off is not None and self.offset == 0:
                    self.offset = off               # 校正时钟后立刻重试
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                # 重建异常时 fp 必须是文件对象：直接塞 bytes 会让 e.read() 炸出
                # AttributeError（'bytes' object has no attribute 'read'），把真正的 S3
                # 错误码一起埋掉 —— 排查时看不到错因，比报错本身更误事。
                raise urllib.error.HTTPError(e.url, e.code, e.msg, e.headers,
                                             io.BytesIO(body))
        raise RuntimeError("重试次数用尽：%s %s" % (method, key))

    def head(self, key):
        """返回 (存在?, size, etag)。404/403 都算「取不到」，与处理脚本同口径。"""
        try:
            r = self._call("HEAD", key)
            size = int(r.headers.get("Content-Length") or 0)
            etag = (r.headers.get("ETag") or "").strip().strip('"')
            r.close()
            return True, size, etag
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return False, 0, ""
            raise

    def exists(self, key):
        try:
            return self.head(key)[0]
        except Exception:
            return False

    def get_range(self, key, first, last):
        r = self._call("GET", key, rng="bytes=%d-%d" % (first, last))
        code = getattr(r, "status", 200)
        data = r.read()
        r.close()
        if code != 206:
            # 不认 Range 却继续读，就变成整份下载：百万级目录会把流量打爆，直接中止
            raise RuntimeError("网关对 Range 回了 %s 而不是 206（不认区间读）。"
                               "请改用 --verify size，或明确接受整份读就用 --verify full" % code)
        return data

    def get_all(self, key):
        r = self._call("GET", key)
        data = r.read()
        r.close()
        return data

    def get_to_file(self, key, path, chunk=1 << 20):
        """流式下载到本地（备份件里有几百兆的，不能整份读进内存）。
           先写 .part 再改名：中途挂了不能留下一个“看着像备份成功”的半截文件；
           边写边算 MD5，不用为了核对再读一遍。返回 (字节数, md5)。"""
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = path + ".part"
        h = hashlib.md5()
        size = 0
        r = self._call("GET", key)
        try:
            with io.open(tmp, "wb") as f:
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    size += len(buf)
                    h.update(buf)
                    f.write(buf)
        except Exception:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        finally:
            r.close()
        if os.path.exists(path):
            os.remove(path)          # 新件已下完并验过尺寸才换掉旧名
        os.rename(tmp, path)
        return size, h.hexdigest()

    def copy(self, src_key, dst_key):
        """同桶复制。x-amz-copy-source 进签名，避免未签头被网关挑刺。"""
        source = "/%s/%s" % (self.bucket, strict_encode_path(src_key))
        r = self._call("PUT", dst_key, sign_extra={"x-amz-copy-source": source})
        body = r.read()
        r.close()
        if b"<Error>" in body:
            raise RuntimeError("COPY 失败：%s" % _err_code(body))
        m = re.search(rb"<ETag>\s*\"?([^<\"]+)", body)
        return m.group(1).decode("utf-8", "replace").strip() if m else ""

    def delete(self, key):
        r = self._call("DELETE", key)
        r.read()
        r.close()
        return getattr(r, "status", 204)

    def versioning(self):
        """只读探测。开不了版本控制是既定事实，这里只为在预览里把话说明白。"""
        try:
            r = self._call("GET", "", params={"versioning": ""})
            body = r.read()
            r.close()
            if b"<!DOCTYPE" in body:
                return "未知"                    # 禁 DTD/实体解析，与列目录同规矩
            root = ET.fromstring(body)
            return (root.findtext("%sStatus" % _NS) or "Disabled").strip() or "Disabled"
        except Exception:
            return "未知"

    def list_layer(self, key_prefix, suffixes, quiet, max_list, stats, recursive=False):
        if not key_prefix.endswith("/"):
            key_prefix += "/"
        _HB["last"] = 0.0
        out, token = [], None
        scanned = n_copy = pages = prefixes = 0
        t0 = time.time()
        complete = False
        first_key_prev = None
        while True:
            pages += 1
            params = {"list-type": "2", "encoding-type": "url", "max-keys": "1000",
                      "prefix": key_prefix}
            if not recursive:
                params["delimiter"] = "/"            # 只列这一层，子目录归并成 CommonPrefixes
            if token:
                params["continuation-token"] = token
            resp = self._call("GET", "", params=params)
            body = resp.read()
            resp.close()
            # 响应虽来自自有 MinIO，仍禁 DTD/实体解析（与备份脚本同规矩）
            if b"<!DOCTYPE" in body or b"&" in body.replace(b"&amp;", b"").replace(
                    b"&lt;", b"").replace(b"&gt;", b"").replace(b"&quot;", b""):
                raise RuntimeError("列目录响应含非法 DTD/实体，已中止")
            root = ET.fromstring(body)
            page_first = None
            for c in root.findall("%sContents" % _NS):
                key = urllib.parse.unquote(c.findtext("%sKey" % _NS, ""))
                size = int(c.findtext("%sSize" % _NS, "0") or 0)
                etag = (c.findtext("%sETag" % _NS, "") or "").strip().strip('"')
                if page_first is None:
                    page_first = key
                out.append((key, size, etag))
                scanned += 1
                if suffixes and os.path.splitext(key)[0].endswith(tuple(suffixes)):
                    n_copy += 1
            prefixes += len(root.findall("%sCommonPrefixes" % _NS))
            if root.findtext("%sIsTruncated" % _NS, "false").lower() != "true":
                complete = True
                break
            # 翻页防呆：这一页的首对象和上一页同名，就是 continuation-token 没生效。
            # 网关对无效 token 不报错、直接回第一页，静默下来变成「同一页扫几百遍」：
            # 名单里全是重复行、计数看着很大，其实目录往后一步都没走，配对结论全废。
            # 宁可在这里停住喊人，也不能拿着这种名单去判「可删」。
            if page_first is not None and page_first == first_key_prev:
                raise RuntimeError("翻页失败：第 %d 页与上一页首对象相同（%s），"
                                   "continuation-token 没生效，已中止 —— 继续翻只会把同一页反复扫"
                                   % (pages, page_first))
            first_key_prev = page_first
            if max_list > 0 and scanned >= max_list:
                break
            # 这里漏过 `% _NS`：findtext 的第二个参数是「默认值」，带字面 %s 的标签名永远查不到，
            # 于是 token 一直是空串、下一页又请求回第一页 —— 6.1 那次「扫到 5,475,766 个对象、翻 19,697 页」
            # 其实是同一页（278 个对象）被扫了 19,697 遍（19,697×278=5,475,766、19,697×50=984,850 均恰好对上）。
            # pdf_backup_sssss.py 里同一个错已一并修。上面那道「页首对象重复即中止」就是为这个而加的。
            token = urllib.parse.unquote(root.findtext("%sNextContinuationToken" % _NS, ""))
            stats.update(scanned=scanned, n_copy=n_copy, pages=pages, prefixes=prefixes, t0=t0)
            hb_scan(quiet, scanned, n_copy, prefixes, pages, t0, suffixes)
        stats.update(scanned=scanned, n_copy=n_copy, pages=pages, prefixes=prefixes, t0=t0)
        hb_scan(quiet, scanned, n_copy, prefixes, pages, t0, suffixes, force=True)
        out.sort()
        return out, complete, prefixes


def hb_scan(quiet, scanned, n_copy, prefixes, pages, t0, suffixes, force=False):
    """列目录心跳：前台原地刷一行，重定向时每 _HEARTBEAT 秒报整行。"""
    now = time.time()
    if not force and now - _HB["last"] < _HEARTBEAT:
        return
    _HB["last"] = now
    if force:
        if not quiet:
            with _lock:
                sys.stderr.write("\n")
                sys.stderr.flush()
        return
    rate = scanned / (now - t0) if now > t0 else 0
    line = ("[%s] 列目录中… 已扫 %s 个对象（其中要删的 %s %s 个），翻了 %s 页、遇到子目录前缀 %s 个，"
            "约 %s 个/秒" % (human_secs(now - t0), format(int(scanned), ","),
                            "+".join(suffixes or ()) or "-", format(int(n_copy), ","),
                            format(int(pages), ","), format(int(prefixes), ","),
                            format(int(rate), ",")))
    cut = line if len(line) <= 116 else line[:110] + "…"
    with _lock:
        if quiet:
            sys.stderr.write(cut + "\n")
        else:
            sys.stderr.write("\r" + cut + " " * max(0, 110 - len(cut)))
        sys.stderr.flush()


# ---------------- 预览后到真删前：成品有没有被人换掉 ----------------

def etag_short(etag):
    """报错信息里用简短 ETag，免得整串哈希涌屏"""
    s = str(etag or "").strip('"')
    return (s[:8] + "…") if len(s) > 12 else (s or "无")


def partner_drift(size_then, etag_then, size_now, etag_now):
    """预览之后，膨胀版（成品 X.pdf）是不是已经被人家换过一份。

       体积对不上、或两边都有 ETag 且不相等 → 算变了。ETag 这里**不**做额外归一
       （引号、分片后缀之类），与原件那道复核保持完全口径：宁可把「没变」误判成
       「变了」而少删一个，也不能反过来把真换掉的当成没换 —— 后者删掉的就是唯一
       凭据。不额外发请求：用的是「成品还在吗」那次 HEAD 已有的返回值。

       体积读不成数（垃圾值）也当变了 —— 拿不准就不删。"""
    try:
        if int(size_then) != int(size_now):
            return True
    except (TypeError, ValueError):
        return True
    if etag_then and etag_now and str(etag_then).strip('"') != str(etag_now).strip('"'):
        return True
    return False


# ---------------- 本地备份（--backup-local）：写不进桶时唯一的底 ----------------

def _safe_segment(seg, limit=150):
    """把 key 里不能当文件名的字符换成 _，名字过长时截短并挂哈希后缀。

       消毒不是可选项：桶里真有名字带 `:` 、`;` 的标准件（如 ISO_TR 52022-2-2017;fr_5772.pdf），
       在 Windows 上直接写不出去；而“备份失败”比“没备份”更坑 —— 它会让人以为有底。"""
    s = re.sub(r'[<>:"|?*\\/\x00-\x1f]', "_", str(seg)).strip().rstrip(".") or "_"
    if len(s) > limit:
        stem, ext = os.path.splitext(s)
        s = "%s_~%s%s" % (stem[:limit - 20], hashlib.md5(s.encode("utf-8")).hexdigest()[:8], ext)
    return s


def local_path_for(root, key):
    """备份落点：<root>/<整个 key 路径>，逐段消毒。

       用整个 key 而不是 basename：同一个备份根要给 6.1 / 7.1 / 9.3 共用，
       只留文件名迟早会互相盖名 —— 盖掉的就是「以为有底、其实没了」。"""
    parts = [_safe_segment(x) for x in str(key).strip("/").split("/") if x]
    return os.path.join(root, *parts) if parts else os.path.join(root, "_")


def etag_is_plain_md5(etag):
    """单次 PUT 的 ETag 就是 MD5 十六进制；分片上传形如 "hash-2"，那种不能直接比。"""
    return bool(re.match(r"^[0-9a-fA-F]{32}$", str(etag or "").strip('"')))


def check_backup(path, size, etag):
    """核对已有的本地备份：存在 + 体积对 +（ETag 是 MD5 形态时）摘要对。
       返回 (可用?, 实际字节, md5, 说明)。拿不准一律判不可用 —— 备份是删除前唯一的底。"""
    if not os.path.isfile(path):
        return False, 0, "", "本地没有备份文件"
    try:
        actual = os.path.getsize(path)
    except OSError as e:
        return False, 0, "", "读本地文件失败：%s" % e
    if size and actual != int(size):
        return False, actual, "", "本地体积 %s ≠ 桶内体积 %s" % (actual, size)
    h = hashlib.md5()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    md5 = h.hexdigest()
    if etag_is_plain_md5(etag) and md5 != str(etag).strip('"').lower():
        return False, actual, md5, "本地 MD5 %s ≠ 桶内 ETag %s" % (md5, str(etag).strip('"'))
    return True, actual, md5, ("一致" if etag_is_plain_md5(etag)
                               else "体积一致（ETag 非 MD5 形态，未比摘要）")


# ---------------- 校验：判「X.pdf 能不能当成品」----------------

def load_fitz():
    try:
        import fitz            # PyMuPDF 的包名是 fitz
        return fitz
    except Exception:
        return None


def xref_target_ok(seg):
    """Range 取回的那一段像不像 xref：表头 / N M obj / 含 /Type /XRef 的交叉引用流。"""
    if seg.startswith(b"xref"):
        return True
    if re.match(rb"\d+\s+\d+\s+obj\b", seg):
        return True
    return re.search(rb"/Type\s*/XRef", seg) is not None


def verify_tail(client, key, size, window):
    """只读尾部窗口，返回 (level, code, note)。判不出来一律 susp —— 调用方不删。"""
    try:
        tail = client.get_range(key, max(0, size - window), size - 1)
    except RuntimeError as e:
        return _V_BAD, "RANGE_UNSUPPORTED", str(e)
    except Exception as e:
        return _V_SUSP, "READ_FAIL", "取尾窗失败：%s" % type(e).__name__
    if not tail:
        return _V_BAD, "EMPTY_TAIL", "尾窗是空的"
    if not tail.rstrip().endswith(b"%%EOF"):
        return _V_BAD, "TAIL_NO_EOF", "尾窗末尾不是 %%EOF：文件结尾不完整，当半成品看"
    if _PAD_END not in tail[-256:]:
        return _V_SUSP, "NO_PAD_END", ("末尾 256 字节里没有填充结束标记 %r：不像本管线做的膨胀版"
                                       "（可能是原件或旧规则产物），不删" % _PAD_END)
    hits = _TAIL_XREF_RE.findall(tail) or _TAIL_XREF_ANY_RE.findall(tail)
    if not hits:
        return _V_BAD, "TAIL_NO_STARTXREF", "尾窗 %d 字节里找不到 startxref" % len(tail)
    off = int(hits[-1])
    if off <= 0 or off >= size:
        return _V_BAD, "XREF_OFF_RANGE", "startxref 指向 %d，越出文件长度 %d" % (off, size)
    # 数字认出来了还得验位置：把指针目标段取回来核对，防二进制里偶然出现的字样
    seg_len = min(size - off, 512)
    try:
        seg = client.get_range(key, off, off + seg_len - 1)
    except RuntimeError as e:
        return _V_BAD, "RANGE_UNSUPPORTED", str(e)
    except Exception as e:
        return _V_SUSP, "XREF_READ_FAIL", "核对指针目标失败：%s" % type(e).__name__
    if not xref_target_ok(seg):
        return _V_BAD, "XREF_FATAL", "startxref 指过去不像 xref（%d 处）：成品可能读不到页表" % off
    return _V_OK, "TAIL_OK", "填充标记在位、尾部指针可用（未整份打开）"


def verify_full(client, key, fitz):
    """整份读进内存（不落盘）喂 PyMuPDF：能给出的最强结论。"""
    try:
        data = client.get_all(key)
    except Exception as e:
        return _V_SUSP, "DOWNLOAD_FAIL", "整份读取失败：%s" % type(e).__name__
    if not data.startswith(b"%PDF-"):
        return _V_BAD, "NOT_PDF", "开头没有 %PDF- 魔数"
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return _V_BAD, "OPEN_FAIL", "内核直接打不开：%s: %s" % (type(e).__name__, str(e)[:120])
    try:
        pages = doc.page_count
        repaired = bool(getattr(doc, "is_repaired", False))
    finally:
        try:
            doc.close()
        except Exception:
            pass
    if not pages:
        return _V_BAD, "ZERO_PAGES", "能打开但 0 页"
    if repaired:
        return _V_SUSP, "REPAIRED", "内核走了修复分支才打开（%d 页）：不算坏，但值得看一眼" % pages
    return _V_OK, "CLEAN", "%d 页，内核未走修复" % pages


# ---------------- 分类 ----------------

def classify(objs, suffixes, min_bytes, trash_prefix):
    """按配对关系分四类。key 一律「不含桶名」的路径。

    返回 copies/inflated/cand/no_infl/small/other。cand 里每项是
    (原件key, 原件size, 原件etag, 膨胀版key, 膨胀版size, 膨胀版etag)。"""
    trash_prefix = trash_prefix or ""
    roots, copies, copy_keys = {}, [], set()
    for key, size, etag in objs:
        if key.startswith(trash_prefix) and trash_prefix:
            continue                    # 回收站里的件不参与本轮判断，防止循环删自己
        base, ext = os.path.splitext(key)
        for suf in suffixes:
            if ext.lower() == ".pdf" and base.endswith(suf):
                copies.append((key, size, etag, suf))
                roots[base[:-len(suf)]] = (key, size, etag)
                copy_keys.add(key)
                break
    inflated = {}
    other = 0
    for key, size, etag in objs:
        base, ext = os.path.splitext(key)
        if key.startswith(trash_prefix) and trash_prefix:
            other += 1                  # 回收站对象归入「其他」：只数一下，绝不碰
        elif key in copy_keys:
            continue                    # 原件本身已在 copies 里计过，不重复算
        elif ext.lower() == ".pdf" and base in roots:
            inflated[base] = (key, size, etag)
        else:
            other += 1
    cand, no_infl, small = [], [], []
    for key, size, etag, suf in copies:
        root = os.path.splitext(key)[0][:-len(suf)]
        if root in inflated:
            ikey, isize, ietag = inflated[root]
            if isize < min_bytes:
                small.append((key, size, etag, ikey, isize, ietag))
            else:
                cand.append((key, size, etag, ikey, isize, ietag))
        else:
            no_infl.append((key, size, etag))
    return copies, inflated, cand, no_infl, small, other


# ---------------- 交互闸 ----------------

def gate(apply_mode, yes, lines, action="删除"):
    """真动手前的最后一道闸。

       干跑到这里就必须结束 —— 不能只「跳过确认」然后往下走，那会让干跑把活真干完
       （写这版时就这么错了一次，测试里才抓出来）。非交互必须 --yes，交互要输 yes
       （不是单字母 y，防误按）。"""
    if not apply_mode:
        print("\n干跑结束：本轮只出预览与名单，桶里一个字节都没改。"
              "核对完上面三份名单，重跑加 --apply 才会真%s。" % action)
        sys.exit(0)
    for ln in lines:
        print(ln)
    if yes:
        print("  （已带 --yes：跳过交互确认，直接开始%s）" % action)
        return
    if not (sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        sys.exit("错误：现在不是交互终端，不敢替你做决定。核对完名单后加 --yes 重跑")
    try:
        ans = input("输入 yes 开始%s（其他任意输入放弃）：" % action).strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\n已放弃，桶没动。")
    if ans != "yes":
        print("已放弃，桶没动。")
        sys.exit(0)


# ---------------- 回收站路径 ----------------

def trash_prefix_for(key_prefix, day, trash_dir):
    """回收站放在本目录这一层里：<目录>/_trash_sssss/<日期>/，账号权限一定够用。
       保留原相对路径，避免不同子目录里的同名原件互相覆盖。"""
    return "%s%s/%s/" % (key_prefix.rstrip("/") + "/", trash_dir, day)


def orig_key_from_trash(trash_key, trash_prefix, key_prefix):
    """从回收站对象名反推它在桶里的原位：<目录>/<相对路径>。"""
    rel = trash_key[len(trash_prefix):]
    return "%s%s" % (key_prefix.rstrip("/") + "/", rel)


# ---------------- 名单落盘 ----------------

def write_rows(path, header, rows):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        for h in header:
            f.write("# %s\n" % h)
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")
    return path


def write_names(path, names, header):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        for h in header:
            f.write("# %s\n" % h)
        for n in names:
            f.write(n + "\n")
    return path


class RunLog:
    """执行流水「边做边记」：每件一完成就立刻写进文件并 flush，不攒到整轮结束再写。

       删除是不可逆动作。万一断网、断电、进程被杀，「到底删了哪些、哪些失败、还剩多少」
       必须已经躺在盘上——否则事后既没法对账，也没法安全续跑。旧版本是整轮跑完才由 finish()
       一次性写三份文件，中途出事就一份都没落下来，跟「__main__」那句「已完成的都记在日志里」
       的承诺正好相反。所以三份文件改成开跑前就创建、写好表头，之后逐行追加：
         · log_path   逐行「状态  完成时刻  单件耗时  完整key  原因」，可 grep 可人工回溯；
         · ok_path    每行一个成功对象的完整 key（增量写，能直接喂回 --force-from）；
         · fail_path  失败对象的 key + 原因（首个失败时才建；失败项没被动过，排查后可重跑）。
       close() 再往 log 追加一段汇总（删前/删后计数、释放量）。
    """

    def __init__(self, log_path, ok_path, fail_path, action, total, extra_header=()):
        self.log_path = log_path
        self.ok_path = ok_path
        self.fail_path = fail_path
        self.deleting = (action == "删除")
        self.freed = 0
        self.counts = {"ok": 0, "fail": 0, "skip": 0}
        self._log = io.open(log_path, "w", encoding="utf-8", newline="\n")
        self._ok = io.open(ok_path, "w", encoding="utf-8", newline="\n")
        self._fail = None
        self._log.write(u"# %s流水日志（边做边记：每件完成即落盘，中途断了也已写好的都在）\n" % action)
        self._log.write(u"# 开始 %s　本轮目标 %d 个\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), total))
        for h in extra_header:
            self._log.write(u"# %s\n" % h)
        self._log.flush()
        self._ok.write(u"# %s成功的对象（完整 key，不含桶名），边做边写，本轮目标 %d 个\n"
                       % (action, total))
        self._ok.flush()

    def record(self, st, key, msg, dur=0.0, size=0):
        """把一件的结果立刻落盘。st ∈ ok/fail/skip；key 为完整对象路径；dur 为单件耗时（秒）。"""
        self.counts[st] = self.counts.get(st, 0) + 1
        self._log.write(u"%s\t%s\t%.2fs\t%s\t%s\n" % (
            st.upper(), time.strftime("%Y-%m-%d %H:%M:%S"), dur, key, msg))
        self._log.flush()
        if st == "ok":
            self._ok.write(key + u"\n")
            self._ok.flush()
            if self.deleting:
                self.freed += int(size or 0)
        elif st == "fail":
            if self._fail is None:
                self._fail = io.open(self.fail_path, "w", encoding="utf-8", newline="\n")
                self._fail.write(u"# 失败清单：key\t原因。失败项没被动过，排查后可重跑\n")
            self._fail.write(u"%s\t%s\n" % (key, msg))
            self._fail.flush()

    def close(self, summary_lines):
        self._log.write(u"# ---- 汇总 ----\n")
        for s in summary_lines:
            self._log.write(s + u"\n")
        self._log.flush()
        self._log.close()
        self._ok.write(u"# 实际成功 %d 行\n" % self.counts.get("ok", 0))
        self._ok.flush()
        self._ok.close()
        if self._fail:
            self._fail.close()


# ---------------- 点名强删：名单 → 候选（只做归一，绝不凭名字拼 key） ----------------

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")


def canon_pdf_name(name, suffixes):
    """把名单里的一个写法归一成「成品名 X.pdf」（小写、不带目录）。

       为什么拿成品名当准绳而不是备份件名：本脚本自己的 quarantine 名单里写的就是**成品名**
       （那行取的是 basename(膨胀版key)），而要删的对象却是 X_ssssss.pdf。这两个名字一旦混用，
       「按名单删」就会删到语料本体。所以这里只做一个方向的归一：带 _sssss 的把后缀剥掉，
       不带的原样收下，**绝不反过来给成品名加后缀去凑 key** —— 凑出来的 key 一旦不存在或
       指向另一个对象，代价是永久的。真删时用的仍是候选元组里那个扫出来的 key。"""
    n = os.path.basename(str(name).strip().strip("\"'\ufeff").replace("\\", "/")).lower()
    if not n:
        return ""
    if n.endswith(".pdf"):
        stem = n[:-4]
        for suf in suffixes:
            s = str(suf).lower()
            if s and stem.endswith(s) and len(stem) > len(s):
                return stem[:-len(s)] + ".pdf"
    return n


def load_force_names(path, suffixes):
    """读「点名强删」名单。形式宽容，但结果只有两种：一个成品名，或者跳过（不猜）：
         # 开头 / 空行            → 跳过（本脚本出的名单都带 # 表头）
         含制表符的整行        → 取第一个以 .pdf 结尾的字段（自家 to_delete_*.tsv 能直接吃）
         「名字 → 码（说明）」  → 取箭头前那段（quarantine_*.txt 就是这个格式；要求箭头后
                             紧跟一个全大写校验码才这么切，免得把名字里自带箭头的文件截错）
         其余                  → 整行当名字（可带目录前缀，取 basename）"""
    want = set()
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip().strip("\ufeff").strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                fld = [x.strip() for x in line.split("\t") if x.strip().lower().endswith(".pdf")]
                if not fld:
                    continue
                line = fld[0]
            elif "\u2192" in line:
                head, _, tail = line.rpartition("\u2192")
                code = tail.strip().split("\uff08", 1)[0].split(" ", 1)[0].strip()
                if head.strip().lower().endswith(".pdf") and _CODE_RE.match(code):
                    line = head.strip()
            nm = canon_pdf_name(line, suffixes)
            if nm:
                want.add(nm)
    return want


def pick_forced(force_want, keep, susp, quar, suffixes):
    """名单 → (放行项, 未命中名单)。只在已扫出的候选里按成品名比，绝不凭名字拼 key。

       keep 里的名字也算「命中」（那些本来就要删），这样同一份 to_delete.tsv 喂进来
       不会因为「全都已在校验通过里」而被误判成名单对不上。
       返回的放行项仍是完整候选元组，调用方按 it[0]（<名字>_sssss.pdf）去删。

       force_want 进来还会再归一一次：正常调用方（load_force_names）已经归一过了，
       多归一次是幂等的；但不归的话，将来哪个调用方直接塞原始名字就会静默变成
       「全未命中」——那只会让 --apply 拒掉，不会删错东西，但报错原因会很难看。"""
    want = set(canon_pdf_name(x, suffixes) for x in force_want)
    want.discard("")
    hit = set(canon_pdf_name(os.path.basename(it[3]), suffixes) for it in keep)
    forced = []
    for it in susp + quar:
        nm = canon_pdf_name(os.path.basename(it[3]), suffixes)
        if nm in want:
            forced.append(it)
            hit.add(nm)
    return forced, sorted(want - hit)


# ---------------- 删除主流程 ----------------

def main():
    ap = argparse.ArgumentParser(
        description="删掉「已膨胀成功且成品可用」的 _sssss 备份件（默认干跑，删前先复制进回收站）")
    ap.add_argument("--endpoint", help="【必填】MinIO 地址，如 http://10.89.14.234:12001")
    ap.add_argument("--access-key")
    ap.add_argument("--secret-key")
    ap.add_argument("--prefix", help="【必填】<bucket>/<路径>，不带开头斜杠；不留默认值，"
                                     "防止拿上一个目录的名单删错地方")
    ap.add_argument("--suffix", default=CONFIG["SUFFIX"], help="要删的产物后缀（建议只留 _sssss）")
    ap.add_argument("--verify", choices=("size", "tail", "full"), default="tail",
                    help="校验强度：size=只看体积；tail(默认)=Range 取尾窗验结构；full=整份读内存真开")
    ap.add_argument("--min-inflated", type=int, default=CONFIG["MIN_INFLATED_MIB"], metavar="MIB",
                    help="膨胀版最小体积，低于它判「疑似半成品」不删（默认 5MiB，与处理脚本 _BIG 同口径）")
    ap.add_argument("--tail-window", type=int, default=CONFIG["TAIL_WIN"], metavar="BYTES",
                    help="tail 档读尾部多少字节（默认 24576）")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="full 档只抽验前 N 个（0=全部）：用来给 tail 档定标")
    ap.add_argument("-n", "--limit", type=int, default=0, help="本轮最多处理几个候选（0=不限）")
    ap.add_argument("--max-list", type=int, default=0,
                    help="最多列多少个对象就停（摸底用；停过就拒绝 --apply）")
    ap.add_argument("--only-keys-file",
                    help="只处理这份名单里的名字（兼容备份脚本的 deletable_keys.txt；"
                         "名单里写的必须是备份件名 X_sssss.pdf。本脚本自己的 quarantine_*.txt 写的是"
                         "成品名，那种名单请用 --force-from）")
    ap.add_argument("--force-from", metavar="名单文件",
                    help="点名强删：名单里那些「校验没过、本应隔离」的备份件也放行删除。"
                         "本脚本自己的 quarantine_*.txt / to_delete_*.tsv 可直接喂（每行一个名字也行）。"
                         "它只从本轮已扫出的候选里按名字挑、删的仍是 <名字>_sssss.pdf、照旧先复制进"
                         "回收站；名单里有任何一个名字对不上候选就拒绝 --apply")
    ap.add_argument("--trash-prefix", help="回收站前缀（默认 <本目录>/%s/<今天>/）" % CONFIG["TRASH_DIR"])
    ap.add_argument("--no-trash", action="store_true",
                    help="不复制进回收站、直接删（桶开不了版本控制时这是不可逆操作，需额外确认）")
    ap.add_argument("--backup-local", metavar="目录",
                    help="删前先把原件 GET 到这个目录留底（逐件比过体积与 MD5 才删，对不上就不删）。"
                         "桶不让你 PutObject（建不了回收站、--restore 也用不了）时靠它兜底；"
                         "开了它就自动跳过桶内复制")
    ap.add_argument("-j", "--jobs", type=int, default=CONFIG["JOBS"], help="并发数（默认 4，写操作别开太大）")
    ap.add_argument("--stop-on-error", type=int, default=CONFIG["STOP_ON_ERROR"],
                    help="连续失败达到这个数就中止（默认 5，0=不中止）")
    ap.add_argument("--restore", action="store_true",
                    help="反向操作：把回收站里的原件复制回原位（默认不动回收站副本）")
    ap.add_argument("--apply", action="store_true", help="真动手。不带它就是干跑，一个写请求都不发")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（非交互终端必须带）")
    ap.add_argument("--out", help="名单与日志目录（默认 脚本目录/delete_logs/<目录尾段>）")
    ap.add_argument("-q", "--quiet", action="store_true", help="不原地刷新，只报整行心跳")
    args = ap.parse_args()

    ensure_utf8_streams()
    dd = _dotenv()
    endpoint = _pick(args.endpoint, ("MINIO_ENDPOINT",), ("MINIO_ENDPOINT",), dd) or CONFIG["ENDPOINT"]
    ak = _pick(args.access_key, ("MINIO_ACCESS_KEY",), ("MINIO_ACCESS_KEY", "ACCESS_KEY"), dd)
    sk = _pick(args.secret_key, ("MINIO_SECRET_KEY",), ("MINIO_SECRET_KEY", "SECRET_KEY"), dd)
    prefix = _pick(args.prefix, ("PDF_PREFIX",), ("MINIO_PREFIX",), dd)
    prefix = (prefix or "").strip().strip("/")
    if not endpoint:
        sys.exit("错误：必须 --endpoint（或环境变量 MINIO_ENDPOINT）")
    if not ak or not sk:
        sys.exit("错误：MinIO 凭据不完整。用 --access-key/--secret-key 或环境变量 "
                 "MINIO_ACCESS_KEY/MINIO_SECRET_KEY 或脚本同目录 .env")
    if not prefix or "/" not in prefix:
        sys.exit("错误：--prefix 必须是 <bucket>/<路径>（不留默认值就是防止误删别的目录），当前：%r" % prefix)
    bucket, _, key_prefix = prefix.partition("/")
    if not key_prefix:
        sys.exit("错误：--prefix 里只有桶名没有目录 —— 整桶删除不在本脚本能力范围")
    suffixes = tuple(s.strip() for s in (args.suffix or "").split(",") if s.strip())
    if not suffixes:
        sys.exit("错误：--suffix 不能为空")
    for s in suffixes:
        if len(s) <= 2:
            sys.exit("错误：后缀 %r 太短，删除不接受这种歧义（无法区分「恰好叫 X%s.pdf 的真实文档」）"
                     % (s, s))
    force_want = None
    if args.force_from:
        if not os.path.isfile(args.force_from):
            sys.exit("错误：--force-from 名单文件不存在：%s（路径按当前工作目录解析，"
                     "必要时写绝对路径）" % args.force_from)
        force_want = load_force_names(args.force_from, suffixes)
        if not force_want:
            sys.exit("错误：--force-from 名单里没解析出任何一个名字：%s（只认本脚本出的 "
                     "quarantine_*.txt / to_delete_*.tsv，或每行一个 PDF 名字的纯名单）"
                     % args.force_from)
        print("  点名强删名单：%s 个名字（来自 %s）"
              % (format(len(force_want), ","), os.path.basename(args.force_from)))
    quiet = bool(args.quiet) or not sys.stdout.isatty()
    if args.only_keys_file:
        # 这个检查必须在列目录之前做：以前它拖到 [2/5] 才读文件，名单路径写错时
        # 白白扫完整个目录，然后抛一个裸 traceback（FileNotFoundError）——
        # 看不出是名单写错了，也很容易误判成脚本挂了。
        if not os.path.isfile(args.only_keys_file):
            sys.exit("错误：--only-keys-file 名单文件不存在：%s（路径按当前工作目录解析，"
                     "必要时写绝对路径）" % args.only_keys_file)

    day = time.strftime("%Y%m%d")
    trash = (args.trash_prefix or trash_prefix_for(key_prefix, day, CONFIG["TRASH_DIR"])).strip("/") + "/"
    if args.restore and args.trash_prefix:
        trash = args.trash_prefix.strip("/") + "/"
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "delete_logs",
                                       "_".join([p for p in prefix.split("/") if p][-2:]))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    ts = time.strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^\w.\-]+", "_", os.path.basename(key_prefix.rstrip("/")) or "root")[:40]
    log_path = os.path.join(out_dir, ("pdf_restore_%s_%s.log" if args.restore
                                      else "pdf_delete_%s_%s.log") % (slug, ts))
    min_bytes = max(0, int(args.min_inflated)) * 1024 * 1024
    # 本地留底：往桶里写被拒时唯一走得通的反悔依据（只发 GET，不写桶）。
    backup_root = (args.backup_local or "").strip().strip('"') or None
    if backup_root and args.restore:
        sys.exit("错误：--backup-local 是「删前把原件 GET 到本地」，--restore 是「从回收站复制回桶里」，"
                 "两者不是一回事，不要一起用")
    if backup_root:
        backup_root = os.path.abspath(backup_root)
        try:
            if not os.path.isdir(backup_root):
                os.makedirs(backup_root)
        except OSError as e:
            sys.exit("错误：--backup-local 目录建不出来：%s（%s）" % (backup_root, e))
        if not os.access(backup_root, os.W_OK):
            sys.exit("错误：--backup-local 目录不可写：%s" % backup_root)

    print("连接：%s（AK=%s****%s）" % (
        endpoint, ak[:4],
        "，地址来自 .env/环境变量" if not args.endpoint else "，地址来自命令行 --endpoint"))
    if not args.endpoint:
        print("  提醒：删除是不可逆的，地址建议每次用 --endpoint 显式写上，"
              "别拿着 .env 里的默认地址连到另一套集群去删东西")
    print("目录：%s（只列这一层，子目录一概不碰）" % prefix)
    print("本轮动作：%s ｜ 模式：%s" % ("从回收站恢复原件" if args.restore else "删除 _sssss 原件备份件",
                                       "【真动手 APPLY】" if args.apply else "【干跑：绝不写桶】"))
    if args.restore:
        print("回收站：%s（恢复只把原件复制回原位，不动回收站里的副本）" % trash)
    elif args.backup_local:
        print("本地留底：%s（每件先 GET 到本地、比过体积与 MD5 才删；桶内回收站因此跳过）"
              % os.path.abspath(args.backup_local))
    elif args.no_trash:
        print("⚠ --no-trash：不复制进回收站，直接删 —— 桶开不了版本控制，删掉就找不回来")
    else:
        print("回收站：%s（每个件先复制到这里、核对成功，才删原件）" % trash)
    print("")

    client = S3(endpoint, ak, sk, bucket, CONFIG["RETRIES"])
    vers = client.versioning()
    if not args.apply:
        print("（干跑也会出完整预览和名单；核对完把这条命令加 --apply 重跑即可）\n")

    # ---- 恢复模式：回收站 → 原位 ----
    if args.restore:
        run_restore(client, args, trash, key_prefix, out_dir, log_path, quiet, vers)
        return

    # ---- [1/5] 列目录 ----
    print("[1/5] 列目录（只读，一次翻页拿回名字+体积+ETag）…")
    t0 = time.time()
    scan = {}
    try:
        objs, complete, prefixes = client.list_layer(key_prefix, suffixes, quiet, args.max_list, scan)
    except KeyboardInterrupt:
        print("\n已中断（列目录阶段，用时 %s，扫到 %s 个对象）。桶没动过，本地也没有名单。"
              % (human_secs(time.time() - t0), format(scan.get("scanned", 0), ",")))
        print("  这个目录大就先摸底：--max-list 200000 --verify size（但列不全就不允许 --apply）")
        sys.exit(130)
    print("      完成：%s 个子目录前缀，用时 %s%s"
          % (format(prefixes, ","), human_secs(time.time() - t0),
             "" if complete else "（被 --max-list 截断）"))

    # ---- [2/5] 配对分类 ----
    copies, inflated, cand, no_infl, small, other = classify(objs, suffixes, min_bytes, trash)
    if args.only_keys_file:
        want = set()
        for line in io.open(args.only_keys_file, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#"):
                want.add(os.path.basename(line))
        cand = [c for c in cand if os.path.basename(c[0]) in want]
        print("      按名单过滤：%s 个名字命中 %s 个候选"
              % (format(len(want), ","), format(len(cand), ",")))
    if args.limit and args.limit > 0:
        cand = cand[:args.limit]
    print("[2/5] 配对分类：")
    print("  同层 %s 共 %s 个（%s）" % ("/".join(suffixes), format(len(copies), ","),
                                        human_size(sum(c[1] for c in copies))))
    print("  ├ 可删候选        %s 个（%s）—— 有膨胀版且体积达标" % (
        format(len(cand), ","), human_size(sum(c[1] or 0 for c in cand))))
    print("  ├ 同层没有膨胀版  %s 个 —— 绝不删（重做膨胀版的唯一凭据）" % format(len(no_infl), ","))
    print("  └ 膨胀版体积不足  %s 个 —— 疑似半成品，不删" % format(len(small), ","))
    print("  本层其他对象 %s 个（膨胀版本体/.md/.json/别的 PDF/回收站）：只数了一下" % format(other, ","))
    if prefixes:
        print("  注意：这一层下面还有 %s 个子目录前缀，本脚本不递归，里面的 _sssss 一律不碰"
              % format(prefixes, ","))

    # ---- [3/5] 校验 ----
    print("\n[3/5] 核验膨胀版 X.pdf 是否可用（%s 档）…" % args.verify)
    fitz = None
    if args.verify == "full":
        fitz = load_fitz()
        if fitz is None:
            print("      没装 PyMuPDF（import fitz），自动降级为 tail 档：只验尾部结构、不真开。"
                  "要真验就 pip install pymupdf")
            args.verify = "tail"
    todo = cand if (args.verify != "full" or args.sample <= 0) else cand[:args.sample]
    rest = len(cand) - len(todo)
    per_req = {"size": 0, "tail": 2, "full": 1}[args.verify]
    if per_req:
        print("      代价：%s 份 × %d 个请求/份 ≈ %s 个请求，按 80ms/请求、并发 %d 估约 %s"
              % (format(len(todo), ","), per_req, format(len(todo) * per_req, ","),
                 max(1, args.jobs), human_secs(len(todo) * per_req * 0.08 / max(1, args.jobs))))

    def one(item):
        key, size, etag, ikey, isize, ietag = item
        if args.verify == "size":
            return item, (_V_OK, "SIZE_ONLY", "只看体积 %s，没验结构" % human_size(isize))
        if args.verify == "full":
            return item, verify_full(client, ikey, fitz)
        return item, verify_tail(client, ikey, isize, args.tail_window)

    verdicts = {}
    keep, susp, quar = [], [], []
    if not todo:
        print("      没有要验的候选")
    elif args.jobs <= 1 or ThreadPoolExecutor is None:
        for it in todo:
            k, v = one(it)
            verdicts[k[0]] = v
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 16))) as ex:
            for i, f in enumerate(as_completed([ex.submit(one, it) for it in todo]), 1):
                it, v = f.result()
                verdicts[it[0]] = v
                if i % 500 == 0:
                    print("      已验 %s/%s" % (format(i, ","), format(len(todo), ",")))
    for it in todo:
        v = verdicts[it[0]]
        (keep if v[0] == _V_OK else susp if v[0] == _V_SUSP else quar).append(it)
    if args.verify == "full" and rest > 0:
        print("      full 档抽验 %s 个，其余 %s 个本轮不动（--sample 0 = 全验）"
              % (format(len(todo), ","), format(rest, ",")))
    print("      结论：通过 %s 个；可疑不删 %s 个；不合格不删 %s 个"
          % (format(len(keep), ","), format(len(susp), ","), format(len(quar), ",")))
    for it in (susp + quar)[:5]:
        lv, code, note = verdicts[it[0]]
        print("        不删样本：%s → %s（%s）" % (os.path.basename(it[3]), code, note[:90]))

    # ---- 点名强删：名单命中的隔离件从「不删」挪进「要删」 ----
    #   匹配只拿候选元组里的成品名（it[3]）比，比中的项仍然按 it[0]（那个 _sssss 备份件）去删。
    #   名单里写成品名（quarantine 就是这个写法）或写 X_sssss.pdf 都算同一个，因为两边都先过
    #   canon_pdf_name 归一；但绝不拿名单里的名字去拼 key —— 名字打错只会表现为「没命中」，
    #   而不是删掉一个凑出来的对象。未命中在 --apply 时直接拒，不让人拿半对的名单去删。
    forced = []
    if force_want is not None:
        forced, miss = pick_forced(force_want, keep, susp, quar, suffixes)
        for nm in miss[:10]:
            print("        名单未命中：%s —— 本轮候选里没有这个名字"
                  "（--prefix 写错？名单来自别的目录？还是它本轮没被隔离？）" % nm)
        if len(miss) > 10:
            print("        …未命中共 %d 个" % len(miss))
        if args.apply and miss:
            sys.exit("错误：点名强删名单有 %d 个名字在本轮候选里找不到，拒绝删除 —— 对不上通常是 "
                     "--prefix 写错或名单来自别的目录，这种情况不能「照剩下的删」。"
                     "先不带 --apply 干跑看清命中数，或把名单里确实不该删的那几行去掉再重跑"
                     % len(miss))
        if forced:
            print("      点名强删：%s 个隔离件按名单放行（不删样本：%s）" % (
                format(len(forced), ","),
                "、".join(os.path.basename(it[0]) for it in forced[:3])
                + ("…" if len(forced) > 3 else "")))
        else:
            print("      点名强删：名单 %s 个名字全部命中已通过校验的候选，本轮无需放行新项"
                  % format(len(force_want), ","))

    # ---- [4/5] 预览 + 确认 ----
    forced_keys = set(it[0] for it in forced)
    will = keep + forced
    freed = sum(int(c[1] or 0) for c in will)
    print("\n[4/5] 预览")
    print("  桶版本控制：%s —— %s" % (vers, "已开启：误删可由版本恢复" if vers == "Enabled"
                                      else "开不了：删除是永久的，所以删前一律先留底"
                                      if (backup_root or not args.no_trash)
                                      else "开不了，且回收站与本地留底都没开：删掉的件找不回来"))
    print("  将要删除：%s 个 %s，预计释放 %s" % (
        format(len(will), ","), "/".join(suffixes), human_size(freed)))
    if forced:
        print("    ├ 其中按名单强删 %s 个（校验没过、本应隔离，结论：%s）" % (
            format(len(forced), ","),
            "、".join(sorted(set(verdicts[c[0]][1] for c in forced)))))
    if backup_root:
        print("  本地留底：%s" % backup_root)
        print("    每件先 GET 落盘、比过体积与 MD5 才删；对不上就那一件不删 —— "
              "往桶里写不进去时这是唯一的底")
        if not args.no_trash:
            print("    桶内回收站因此跳过（不再 COPY，也就不会再撞 403）")
    elif not args.no_trash:
        print("  回收站：%s（保留原相对路径；每个件先 COPY 过来、核对成功才删原件）" % trash)
    else:
        print("  ⚠ 已指定 --no-trash：不复制、直接删。这一步之后没有任何回滚手段")
    print("  绝不触碰：膨胀版本体、.md/.json、子目录、同层无膨胀版的 %s 个、体积不足的 %s 个"
          % (format(len(no_infl), ","), format(len(small), ",")))
    if not complete:
        print("  ⚠ 目录没列完（--max-list）：配对结论可能是「没看到」而不是「不存在」")

    to_delete = write_rows(os.path.join(out_dir, "to_delete_%s.tsv" % ts),
                           ["待删清单：原件key 原件大小 原件ETag 膨胀版key 膨胀版大小 膨胀版ETag 校验结论",
                            "生成：%s @ %s" % (os.path.basename(__file__),
                                             time.strftime("%Y-%m-%d %H:%M:%S")),
                            "干跑也会出这份文件；核对它，就是核对全流程"],
                           [(c[0], c[1], c[2], c[3], c[4], c[5],
                             "%s/%s%s" % (verdicts[c[0]][0], verdicts[c[0]][1],
                                          " ←名单强删" if c[0] in forced_keys else "")) for c in will])
    pend_name = "pending_inflated_keys_%s.txt" % ts
    pending = write_names(os.path.join(out_dir, pend_name),
                          [os.path.basename(k) for k, s, e in no_infl] +
                          [os.path.basename(c[0]) for c in small],
                          ["同层没有膨胀版（或膨胀版体积不足）的 %s：重做膨胀版的唯一凭据，绝对不能删。"
                           % "/".join(suffixes),
                           "每行一个名字，可直接喂处理脚本：python pdf_rename_inflate_upload.py "
                           "--prefix \"<同一个目录>\" --files-from \"%s\"" % pend_name,
                           "共 %d 行。" % (len(no_infl) + len(small))])
    rest_quar = [v for v in susp + quar if v[0] not in forced_keys]
    quar_path = write_names(os.path.join(out_dir, "quarantine_%s.txt" % ts),
                            ["%s → %s（%s）" % (os.path.basename(v[3]), verdicts[v[0]][1], verdicts[v[0]][2])
                             for v in rest_quar],
                            ["校验没过或拿不准的候选：本轮不删，人工看一眼。共 %d 行%s"
                             % (len(rest_quar),
                                "（另有 %d 个已按名单强删，记在 to_delete 那份里）"
                                % len(forced) if forced else "")])
    print("  名单已落盘：\n    待删   %s\n    待补膨胀版 %s\n    隔离待看 %s" % (to_delete, pending, quar_path))

    # 列目录被 --max-list 截断过，配对结论就不成立：“没看到膨胀版”可能只是“没列到”。
    # 这里直接拒掉写操作，不能只靠预览里一句警告让人自己把握
    if args.apply and not complete:
        sys.exit("错误：目录没列完（--max-list），配对结论可能是「没看到」而不是「不存在」，"
                 "拒绝删除。去掉 --max-list 重跑，或用 --only-keys-file 只按备份脚本已核对的名单删")
    # 备份写到一半才发现盘满是最坏的处境：前一半已经删了、后一半还没底。
    # 所以先按本轮名单体积要一次空间（再加 10% 余量），不够就直接拒 --apply。
    if backup_root and args.apply:
        need = sum(int(c[1] or 0) for c in will)
        free = shutil.disk_usage(backup_root).free
        if free < need + need // 10:
            sys.exit("错误：本地留底目录 %s 只剩 %s，装不下本轮需要留底的 %s（还要留 10%% 余量）。"
                     "换个盘，或用 --only-keys-file 缩小范围再来"
                     % (backup_root, human_size(free), human_size(need)))
    gate(args.apply, args.yes, [
        "\n再次确认：即将从 %s 真删 %s 个文件（释放 %s）。" % (
            prefix, format(len(will), ","), human_size(freed)),
        "  桶版本控制：%s；%s" % (vers,
                                 "每件先 GET 到本地留底 %s 再删" % backup_root if backup_root
                                 else "每个件会先复制进 %s 再删" % trash if not args.no_trash
                                 else "回收站已关闭，删掉就找不回来"),
        ("  （往桶里写不了时，本地留底就是唯一的反悔机会；每件都要比过体积与 MD5 才删）"
         if backup_root else
         "  （桶开不了版本控制，回收站就是唯一的反悔机会 —— 除非磁盘极紧，不建议 --no-trash）")
        + ("\n  ⚠ 其中 %s 个是校验没过的隔离件，凭 --force-from 名单放行：%s"
           % (format(len(forced), ","),
              "它们的原件备份已 GET 到本地留底，重做依据在磁盘上" if backup_root
              else "删掉后就再无重做依据"))],
        action="删除")

    # ---- [5/5] 执行：COPY → 核对副本 → DELETE → 确认消失 ----
    print("\n[5/5] 开始执行…")
    backup_rows = []      # --backup-local：(桶内 key, 本地路径，体积，md5)
    size_map = dict((c[0], int(c[1] or 0)) for c in will)   # 预先建索引，不随候选数线性重算
    n_before = len(will)      # 删前计数（写进汇总跟删后对比）
    # 三份文件开跑前就建好、写表头，之后每件一完成逐行追加+flush（见 RunLog）：
    #   删除不可逆，中途断网/断电/被杀时「已删了哪些」必须已经在盘上，才对账得上。
    w = RunLog(log_path,
               os.path.join(out_dir, "deleted_ok_keys_%s.txt" % ts),
               os.path.join(out_dir, "delete_failed_%s.tsv" % ts),
               "删除", n_before,
               ["回收站：%s　本地留底：%s" % (
                   "已关闭（无桶内副本）" if (args.no_trash or backup_root) else trash,
                   backup_root or "无"),
                "删前待删 %d 个；逐件记录状态/完成时刻/单件耗时/完整key/原因" % n_before])

    def step(item):
        key, size, etag, ikey, isize, ietag = item
        name = os.path.basename(key)
        if _abort.is_set():
            return None
        # 查 1：原件还在，且大小/ETag 与预览时一致（一致才说明中间没人动过）
        try:
            ex_, esize, eetag = client.head(key)
        except Exception as e:
            return ("fail", key, "HEAD 原件异常：%s" % e)
        if not ex_:
            return ("skip", key, "原件已不存在（上轮删过或别人删了），跳过")
        if esize != size or (etag and eetag and etag != eetag):
            return ("fail", key, "预览后对象变了（大小 %s→%s、ETag %s→%s），停手不删"
                    % (size, esize, etag, eetag))
        # 查 2：对应的膨胀版此刻仍在，而且仍是预览时那一份
        try:
            ok_i, isize_now, ietag_now = client.head(ikey)
        except Exception as e:
            return ("fail", key, "确认膨胀版时 HEAD 异常：%s" % e)
        if not ok_i:
            return ("fail", key, "膨胀版 %s 不见了！原件不删" % os.path.basename(ikey))
        if partner_drift(isize, ietag, isize_now, ietag_now):
            # 只在存在性之外多对一次体积/ETag，不额外发请求（这次 HEAD 本就要发）
            return ("fail", key, "预览后膨胀版被人动过（体积 %s→%s、ETag %s→%s），不删"
                    % (isize, isize_now, etag_short(ietag), etag_short(ietag_now)))
        # 本地留底（--backup-local）：GET 落盘 → 比体积、MD5 → 才允许往下走。
        # 位置必须在 COPY/DELETE 之前：这一步一过，删掉的件就确有底；
        # 顺序反了就变成“先删再试试看能不能救”。
        bnote = ""
        if backup_root:
            lp = local_path_for(backup_root, key)
            usable, _, md5, _why = check_backup(lp, size, etag)
            if usable:
                bnote = "本地已有备份且校验一致，未重复下载"
            else:
                try:
                    got, md5 = client.get_to_file(key, lp)
                except Exception as e:
                    return ("fail", key, "本地留底失败，原件不删：%s" % e)
                if size and got != int(size):
                    return ("fail", key, "本地留底体积 %s ≠ 桶内 %s，原件不删" % (got, size))
                if etag_is_plain_md5(etag) and md5 != str(etag).strip('"').lower():
                    return ("fail", key, "本地留底 MD5 %s ≠ 桶内 ETag %s，原件不删"
                            % (md5, str(etag).strip('"')))
                bnote = "已留底 %s" % os.path.basename(lp)
            backup_rows.append((key, lp, size, md5))
        # 回收站：先复制 + 核对（桶开不了版本控制，这一步就是唯一的反悔机会）
        # 保留原相对路径，不同子目录里的同名原件才不会互相盖名
        rel = key[len(key_prefix) + 1:] if key.startswith(key_prefix + "/") \
            else os.path.basename(key)
        tk = trash + rel
        if not args.no_trash and not backup_root:
            try:
                client.copy(key, tk)
            except urllib.error.HTTPError as e:
                # 只印 str(e) 会把错因埋掉：同样是 403，AccessDenied（没写权限）、
                # SignatureDoesNotMatch（签名/时钟）和网关自己拦下来，处置办法完全两样。
                # _call 已经把响应体塞回 fp 了，这里读它就行。
                detail = _err_code(e.read())
                if e.code == 403:
                    _abort.set()      # 权限类错不会重试重试就好了，别把整批白碰一遍
                    # 这里一定要印完整 key：只印 basename 看不出被套在哪一层前缀里，
                    # 而排查权限靠的就是这个完整落点形状（别拿它换短路径）。
                    print("\n      ✕ COPY 被拒 403：%s\n"
                          "        写去处：%s\n"
                          "        后面的不试了：这一步过不去就绝不删，先查权限或网关。"
                          % (detail, tk))
                return ("fail", key, "复制进回收站失败，原件不删：HTTP %s：%s"
                        % (e.code, detail))
            except Exception as e:
                return ("fail", key, "复制进回收站失败，原件不删：%s" % e)
            try:
                ck, csize, cetag = client.head(tk)
            except Exception as e:
                return ("fail", key, "回收站副本核对时 HEAD 异常，原件不删：%s" % e)
            if not ck or csize != size:
                return ("fail", key, "回收站副本对不上（存在=%s 大小 %s≠%s），原件不删"
                        % (ck, csize, size))
        # 删
        try:
            client.delete(key)
        except urllib.error.HTTPError as e:
            return ("fail", key, "DELETE 失败 HTTP %s：%s" % (e.code, _err_code(e.read())))
        except Exception as e:
            return ("fail", key, "DELETE 异常：%s" % e)
        # 查 3：确认已消失（DELETE 对不存在的对象也回 204，不能靠返回码判成败）
        try:
            still = client.exists(key)
        except Exception:
            still = False
        if still:
            return ("fail", key, "删完对象还在，网关没接受（%s）"
                    % ("本地留底 %s" % os.path.basename(lp) if backup_root
                       else "回收站副本 %s 已留" % tk))
        if backup_root:
            tail = "；%s" % bnote
        elif args.no_trash:
            tail = ""
        else:
            tail = "；回收站 %s" % tk
        return ("ok", key, "已删（释放 %s；膨胀版 %s 在位%s）"
                % (human_size(size), os.path.basename(ikey), tail))

    def timed(it):
        """跑一件并量它的墙上耗时（秒）。step 内部已把网络/权限异常都转成 ('fail', …)，
           这里只负责给它包一层计时，好让“每件多久”能落到日志里。"""
        t = time.time()
        r = step(it)
        return (r, time.time() - t)

    try:
        items = will
        if args.jobs <= 1 or ThreadPoolExecutor is None:
            # 单线程也必须走同一套计数与闸口：不然删完了统计还是 0，看着像没删（比报错更坑）
            for it in items:
                r, dur = timed(it)
                if r is None:
                    break
                w.record(r[0], r[1], r[2], dur, size_map.get(r[1], 0))
                tally(r[0], size_map.get(r[1], 0))
                if args.stop_on_error and _run["consec"] >= args.stop_on_error:
                    print("\n      连续 %d 个失败，先停手（剩下的没动，原因见日志）" % _run["consec"])
                    _abort.set()
                    break
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 16))) as ex:
                futs = [ex.submit(timed, it) for it in items]
                for i, f in enumerate(as_completed(futs)):
                    r, dur = f.result()
                    if r is None:
                        break
                    # 逐行落盘：as_completed 在主线程串行消费，文件写入不会交错
                    w.record(r[0], r[1], r[2], dur, size_map.get(r[1], 0))
                    with _lock:
                        tally(r[0], size_map.get(r[1], 0))
                        if args.stop_on_error and _run["consec"] >= args.stop_on_error:
                            print("\n      连续 %d 个失败，先停手（剩下的没动，原因见日志）"
                                  % _run["consec"])
                            _abort.set()
                            break
                    if (i + 1) % 100 == 0:
                        print("      已处理 %s/%s（成功 %s 跳过 %s 失败 %s）"
                              % (format(i + 1, ","), format(len(items), ","),
                                 _run["ok"], _run["skip"], _run["fail"]))
    except KeyboardInterrupt:
        _abort.set()
        print("\n中断：本轮已处理 %s 个就停了，剩下的没动（已记的每件都已在盘上）。"
              "重跑会自动接着（已删的记为跳过）。"
              % format(_run["ok"] + _run["skip"] + _run["fail"], ","))
    # 本地留底清单：事后要知道“哪个 key 在磁盘哪个位置、MD5 是多少”，
    # 不写这份就等于“底是有的，但没人说得清在不在”。
    if backup_rows:
        bp = write_rows(os.path.join(out_dir, "backup_manifest_%s.tsv" % ts),
                        ["本地留底清单：桶内key 本地路径 体积 md5",
                         "拿这份核对磁盘上的底。要从本地补回桶里需要 PutObject（当前账号多半没有），"
                         "真到那步把整个目录交给运维即可"],
                        backup_rows)
        print("  本地留底清单：%s（%s 件）" % (bp, format(len(backup_rows), ",")))
    finish(w, _run, len(copies), len(no_infl), len(small), "删除", trash,
           args.no_trash, backup_root, n_before)


def tally(st, size):
    if st == "ok":
        _run["ok"] += 1
        _run["bytes"] += int(size or 0)
        _run["consec"] = 0
    elif st == "skip":
        _run["skip"] += 1
        _run["consec"] = 0
    else:
        _run["fail"] += 1
        _run["consec"] += 1


def finish(w, run, n_copies, n_pending, n_small, action, trash, no_trash,
           backup_root=None, n_before=None):
    """收尾：往逐件流水日志末尾追加一段汇总（正文已由 RunLog 在跑的过程中边做边记），
       再打印回滚指引。不再在这里重建日志/成功清单/失败清单——那些从开跑第一件起就在盘上了。"""
    deleting = action == "删除"
    freed = w.freed
    ok_n, fail_n, skip_n = run["ok"], run["fail"], run["skip"]
    summary = [
        "# 成功 %d，跳过 %d，失败 %d，%s" % (
            ok_n, skip_n, fail_n,
            ("实际释放 %s" % human_size(freed)) if deleting
            else ("本轮处理 %d 个" % n_copies)),
    ]
    if deleting and n_before is not None:
        # 删前/删后计数：剩下没删成的 = 跳过 + 失败 + 停手时还没轮到的（n_before-ok_n）
        summary.append("# 删前待删 %d，本轮删成 %d，未删成 %d（含跳过 %d／失败 %d／停手没动的）"
                       % (n_before, ok_n, n_before - ok_n, skip_n, fail_n))
    if deleting:
        summary.append("# 同层无膨胀版（没动过）%d；膨胀版体积不足（没动过）%d"
                       % (n_pending, n_small))
    summary.append("# 回收站：%s%s" % (
        "已关闭（无桶内副本）" if (no_trash or backup_root) else trash,
        "；本地留底：%s" % backup_root if backup_root else ""))
    summary.append("# 结束 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w.close(summary)

    print("\n结果：成功 %s 个%s；跳过 %s；失败 %s"
          % (format(ok_n, ","),
             "（释放 %s）" % human_size(freed) if deleting else "",
             skip_n, fail_n))
    print("  完整日志：%s（每件一行：状态/完成时刻/单件耗时/完整key/原因，边做边记）" % w.log_path)
    print("  成功清单：%s（边做边写）%s" % (w.ok_path,
          "；失败清单：%s" % w.fail_path if fail_n else ""))
    if ok_n and deleting and backup_root:
        print("  ↩ 本轮 %s 件的底在本地：%s（桶里没有副本，别用 --restore；"
              "逐件路径与 MD5 见 backup_manifest_*.tsv）"
              % (format(ok_n, ","), backup_root))
    elif ok_n and deleting and no_trash:
        # --no-trash 下根本没有副本，这句指引要是照旧印“想反悔：--restore …”
        # 就是给人一个假希望：--restore 靠把副本复制回原位，而本轮压根没复制过。
        print("  ⚠ 本轮是 --no-trash：桶里没留下任何副本（%s 里也没有）—— "
              "别指望 --restore：它要把副本复制回原位，而本轮压根没复制过。"
              "要恢复只能从别处重新拿到原件本身" % trash)
    elif ok_n and deleting:
        print("  想反悔：%s --restore --trash-prefix \"%s\" --apply"
              % (os.path.basename(__file__), trash))
    elif ok_n:
        print("  回收站里的副本一份没删（避免搬两次丢两件）；确认新址无误后，可让运维整段清 %s"
              % trash)
    if fail_n:
        print("  有失败项：先看失败清单再重跑，别连着猛试 —— 连续失败通常是凭据、权限或网关在出问题")


def run_restore(client, args, trash, key_prefix, out_dir, log_path, quiet, vers):
    """把回收站里的原件复制回原位。要递归列，因为回收站按相对路径分了子目录。"""
    print("[1/3] 列回收站 %s（递归）…" % trash)
    scan = {}
    t0 = time.time()
    try:
        objs, complete, prefixes = client.list_layer(trash, None, quiet, args.max_list,
                                                     scan, recursive=True)
    except KeyboardInterrupt:
        print("\n已中断（列回收站 %s）；桶没动过。" % human_secs(time.time() - t0))
        sys.exit(130)
    items = [(k, s, e) for k, s, e in objs if os.path.splitext(k)[0].endswith(tuple(
        [x.strip() for x in (args.suffix or "").split(",") if x.strip()]))]
    print("      回收站里有 %s 个 %s 对象（另有 %s 个其他对象不动）"
          % (format(len(items), ","), args.suffix, format(len(objs) - len(items), ",")))

    todo = []
    for k, s, e in items[:args.limit or len(items)]:
        orig = orig_key_from_trash(k, trash, key_prefix)
        todo.append((k, s, e, orig))
    print("[2/3] 预览：将把 %s 个对象复制回原位（原位已有对象的跳过，绝不覆盖）" % format(len(todo), ","))
    if not complete:
        print("  ⚠ 回收站没列完（--max-list），本轮只做已列到的这部分")
    rows = write_rows(os.path.join(out_dir, "to_restore_%s.tsv" % time.strftime("%Y%m%d%H%M%S")),
                      ["恢复清单：回收站key 大小 原位key", "原位已有对象的不列入"],
                      [(k, s, o) for k, s, e, o in todo])
    print("  名单：%s" % rows)
    gate(args.apply, args.yes, ["\n再次确认：即将向 %s 复制回去 %s 个对象（版本控制：%s）。"
                                % (key_prefix, format(len(todo), ","), vers)], action="恢复")

    def step(item):
        tk, size, etag, orig = item
        if _abort.is_set():
            return None
        try:
            ex_, esize, _ = client.head(orig)
        except Exception as e:
            return ("fail", orig, "HEAD 原位异常：%s" % e)
        if ex_:
            return ("skip", orig, "原位已有对象（%s），不覆盖" % human_size(esize))
        try:
            client.copy(tk, orig)
        except urllib.error.HTTPError as e:
            # 恢复路径比删除更需要看到真错因：东西已经不在原位了，不能只回一句 403
            return ("fail", orig, "复制失败：HTTP %s：%s" % (e.code, _err_code(e.read())))
        except Exception as e:
            return ("fail", orig, "复制失败：%s" % e)
        try:
            ok2, s2, _ = client.head(orig)
        except Exception as e:
            return ("fail", orig, "复制后核对异常：%s" % e)
        if not ok2 or s2 != size:
            return ("fail", orig, "复制后大小不对（%s≠%s）" % (s2, size))
        return ("ok", orig, "已恢复到原位（回收站副本仍留在 %s）" % tk)

    # 与删除同样边做边记：恢复也可能中途断，三份文件开跑前就建好、逐行追加。
    ts = (re.search(r"(\d{14})", os.path.basename(log_path)) or
          [None, time.strftime("%Y%m%d%H%M%S")])[1]
    w = RunLog(log_path,
               os.path.join(out_dir, "restored_ok_keys_%s.txt" % ts),
               os.path.join(out_dir, "restore_failed_%s.tsv" % ts),
               "恢复", len(todo),
               ["回收站：%s（恢复只把原件复制回原位，不动回收站副本）" % trash])

    def timed(it):
        t = time.time()
        r = step(it)
        return (r, time.time() - t)

    try:
        if args.jobs <= 1 or ThreadPoolExecutor is None:
            for it in todo:
                r, dur = timed(it)
                if r is None:
                    break
                w.record(r[0], r[1], r[2], dur)
                tally(r[0], 0)
                if args.stop_on_error and _run["consec"] >= args.stop_on_error:
                    _abort.set()
                    break
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 16))) as ex:
                for i, f in enumerate(as_completed([ex.submit(timed, it) for it in todo])):
                    r, dur = f.result()
                    if r is None:
                        break
                    w.record(r[0], r[1], r[2], dur)
                    tally(r[0], 0)
                    if args.stop_on_error and _run["consec"] >= args.stop_on_error:
                        _abort.set()
                        break
    except KeyboardInterrupt:
        _abort.set()
        print("\n中断：本轮已处理 %s 个就停了（已记的每件都已在盘上），剩下的没动。"
              % format(_run["ok"] + _run["skip"] + _run["fail"], ","))
    finish(w, _run, len(todo), 0, 0, "恢复", trash, False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n已中断。动作是逐个做的：已完成的都记在日志里，没做的一个没动。")
    except urllib.error.HTTPError as e:
        sys.exit("错误：请求失败 HTTP %s：%s" % (e.code, _err_code(e.read())))
    except urllib.error.URLError as e:
        sys.exit("错误：连不上 MinIO：%s（地址、端口、服务是否在跑、要不要走代理）" % e.reason)
    except RuntimeError as e:
        sys.exit("错误：%s" % e)
