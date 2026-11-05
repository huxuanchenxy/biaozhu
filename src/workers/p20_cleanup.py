# -*- coding: utf-8 -*-
"""dd 桶 P20* 13 目录「已分类 PDF + MD/images」删除：清单导出 + 白名单删除。

口径（与 stats_p20_classified_run.py 一致）：
  分类 = dd organize_records/*.jsonl.gz 中 committed=true 的 f（按 f 去重，
         跳 d/_summary 行），且 f 以 P20 开头（顶层目录名）。
  删 = 已分类文档的 {stem}.pdf + 整个 {stem}/MD/images/（含全部图片）
  留 = {stem}/MD/{stem}.md；B/C 型平铺 {stem}.md 也保留
      QA/CoT/Json/SFT 默认不动
  文档兜底：同一文档目录先收集所有 .md，候选（pdf/images）只有该文档有 md
            才进待删清单；缺 md 的整篇写 p20_no_md_docs.tsv，绝不删。
  分类外对象（未 committed 的 pdf/images，含 doc 目录内附件）不进删除面，
  只写 p20_unclassified.tsv 供审查。

两种模式：
  export  重新列一遍 P20 目录，产出逐 key 待删清单
          python -X utf8 -u p20_cleanup.py export
          python -X utf8 -u p20_cleanup.py export --limit-dirs 1   # 试跑
          python -X utf8 -u p20_cleanup.py export --refresh-ledger # 强制重建账本
  delete  按清单逐 key 删除（默认 dry-run，只统计不删）
          python -X utf8 -u p20_cleanup.py delete
          python -X utf8 -u p20_cleanup.py delete --yes --resume
          python -X utf8 -u p20_cleanup.py delete --yes --fresh    # 忽略断点重头删

输出（脚本同目录）：
  p20_delete_list.tsv      待删：bucket / type(pdf|img) / key / size
  p20_keep_md_list.tsv     已分类文档保留的 md（审计）
  p20_no_md_docs.tsv       已分类但缺 MD，跳过不删（逐文档）
  p20_unclassified.tsv     分类外 pdf/images（不删）
  p20_other_keys.tsv       无法归类对象样例（上限 2000 行）
  p20_export_stats.json    每目录 + 合计统计
  p20_delete_ckpt.json     删除断点（offset/total）
  p20_delete_errors.txt    删除失败明细（若有）

依赖：pip install boto3（单文件、自带凭证，拷到任何机器可直接跑）
"""
import argparse
import gzip
import io
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config as BotoConfig

DD_ENDPOINT = 'http://10.89.14.234:12001'
DD_AK = 'NXKKBDFEZP9XNNHVRIHT'
DD_SK = 'Fx0k0Ix2Pab4hkZMOvl3oLLKL08W5ZPVmBiTyJUS'
BUCKET = 'drivdernet_abc'
MATCH = 'P20'                 # 顶层目录前缀（P20* 13 目录）
RECORDS_PREFIX = 'organize_records/'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DELETE_LIST = os.path.join(SCRIPT_DIR, 'p20_delete_list.tsv')
KEEP_MD = os.path.join(SCRIPT_DIR, 'p20_keep_md_list.tsv')
NO_MD = os.path.join(SCRIPT_DIR, 'p20_no_md_docs.tsv')
UNCLASSIFIED = os.path.join(SCRIPT_DIR, 'p20_unclassified.tsv')
OTHER_KEYS = os.path.join(SCRIPT_DIR, 'p20_other_keys.tsv')
EXPORT_STATS = os.path.join(SCRIPT_DIR, 'p20_export_stats.json')
LEDGER_CACHE = os.path.join(SCRIPT_DIR, 'stats_p20_ledger_cache.json')
DELETE_CKPT = os.path.join(SCRIPT_DIR, 'p20_delete_ckpt.json')
DELETE_ERRORS = os.path.join(SCRIPT_DIR, 'p20_delete_errors.txt')
PARTS_DIR = os.path.join(SCRIPT_DIR, 'p20_parts')

WORKERS = 13          # export：并行目录数（13 个 P20 目录，已是上限）
LEDGER_WORKERS = 16   # 账本 gz 并行读
DELETE_WORKERS = 16   # delete：delete_objects 批并发（连接池）
BATCH = 1000          # delete_objects 单批硬上限
MAX_INFLIGHT = 16     # delete 在途批数
OTHER_SAMPLE_MAX = 2000
PROGRESS_EVERY = 100000
SEGMENTS = ('/qa/', '/cot/', '/json/', '/sft/')

_lock = threading.Lock()


def get_s3(workers):
    return boto3.client(
        's3', endpoint_url=DD_ENDPOINT,
        aws_access_key_id=DD_AK, aws_secret_access_key=DD_SK,
        config=BotoConfig(
            retries={'max_attempts': 4, 'mode': 'adaptive'},
            max_pool_connections=workers + 8,
            connect_timeout=15, read_timeout=300),
        region_name='us-east-1')


def retry_call(fn, *a, **kw):
    last = None
    for attempt in range(3):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                wait = (attempt + 1) * 2
                print('  [重试] %s %d秒后 (%d/3)' % (e, wait, attempt + 1), flush=True)
                time.sleep(wait)
    raise last


def iter_keys_with_size(s3, prefix, max_retries=3):
    token = None
    while True:
        params = {'Bucket': BUCKET, 'Prefix': prefix, 'MaxKeys': 1000}
        if token:
            params['ContinuationToken'] = token
        resp = None
        for attempt in range(max_retries):
            try:
                resp = s3.list_objects_v2(**params)
                break
            except Exception as e:  # noqa: BLE001
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 2
                    print('  [重试] list %s... %s, %d秒后 (%d/%d)' % (
                        prefix[:40], e, wait, attempt + 1, max_retries), flush=True)
                    time.sleep(wait)
                else:
                    raise
        for obj in resp.get('Contents', []):
            yield obj['Key'], obj.get('Size')
        if not resp.get('IsTruncated'):
            return
        token = resp.get('NextContinuationToken')
        if not token:
            return


def load_committed(s3, ledger_workers=LEDGER_WORKERS, refresh=False,
                   max_age_min=120):
    """dd 全部 organize_records 账本 → (committed pdf key 集合, 统计)。

    只收 P20 前缀行（f.startswith('P20')）、按 f 去重，含本地缓存。
    缓存文件名与 stats_p20_classified_run.py 共用，避免重复重建。"""
    if not refresh and os.path.exists(LEDGER_CACHE):
        age_min = (time.time() - os.path.getmtime(LEDGER_CACHE)) / 60.0
        if age_min > max_age_min:
            print('账本缓存已 %.0f 分钟（> %d），自动重建' % (age_min, max_age_min),
                  flush=True)
        else:
            try:
                with io.open(LEDGER_CACHE, encoding='utf-8') as fh:
                    cached = json.load(fh)
                committed = set(cached['committed'])
                print('账本缓存命中: %d committed（%s 生成，%.0f 分钟前）' % (
                    len(committed), cached.get('generated_at'), age_min), flush=True)
                return committed, cached['stats']
            except Exception as e:  # noqa: BLE001
                print('账本缓存损坏，忽略并重建: %s' % e, flush=True)

    keys = []
    for k, _size in iter_keys_with_size(s3, RECORDS_PREFIX):
        if k.endswith(('.jsonl', '.jsonl.gz')):
            keys.append(k)
    kset = set(keys)
    plain = [k for k in keys if k.endswith('.jsonl') and k[:-6] + '.jsonl.gz' in kset]
    read_keys = sorted(k for k in keys if k not in set(plain))
    print('账本对象=%d（读取 %d，跳过 legacy 明文 %d，%d 并行）' % (
        len(keys), len(read_keys), len(plain), ledger_workers), flush=True)

    def _load_one(k):
        def _fetch():
            raw = s3.get_object(Bucket=BUCKET, Key=k)['Body'].read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return raw.decode('utf-8')
        text = retry_call(_fetch)
        got, rows_local, src_local = [], 0, Counter()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get('d') or '_summary' in obj:
                continue
            rows_local += 1
            e = obj.get('e') or {}
            if not e or not e.get('committed'):
                continue
            f = obj.get('f', '')
            if not f.startswith(MATCH):
                continue
            got.append(f)
            src_local[str(e.get('src_bucket'))] += 1
        return k, got, rows_local, src_local

    committed = set()
    src_cnt = Counter()
    rows_total = rows_p20 = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=ledger_workers) as ex:
        futs = {ex.submit(_load_one, k): k for k in read_keys}
        for i, fut in enumerate(as_completed(futs), 1):
            k, got, rows_local, src_local = fut.result()
            committed.update(got)
            rows_total += rows_local
            rows_p20 += len(got)
            src_cnt.update(src_local)
            print('  [%d/%d] %s rows+=%d committed=%d (%.0fs)' % (
                i, len(read_keys), k.split('/')[-1][:46], rows_local,
                len(committed), time.time() - t0), flush=True)
    stats = {'ledger_objects': len(keys), 'ledger_read': len(read_keys),
             'ledger_plain_skipped': len(plain), 'rows_all': rows_total,
             'rows_p20_committed': rows_p20,
             'committed_unique': len(committed),
             'src_bucket_dist': dict(src_cnt)}
    try:
        with io.open(LEDGER_CACHE, 'w', encoding='utf-8') as fh:
            json.dump({'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'ledger_objects': len(keys),
                       'committed': sorted(committed), 'stats': stats},
                      fh, ensure_ascii=False)
        print('账本缓存已写: %s' % LEDGER_CACHE, flush=True)
    except Exception as e:  # noqa: BLE001
        print('账本缓存写失败（不影响统计）: %s' % e, flush=True)
    return committed, stats


def doc_dir(key):
    """非 pdf key → 所属文档目录（= 同名 pdf 去 .pdf 的路径）。
    A 型：.../{stem}/MD/{stem}.md、.../{stem}/MD/images/*、.../{stem}/{QA|CoT|Json|SFT}/*
    B/C 型：.../{stem}.md（同层平铺，去 .md 当文档目录）
    解析不出返回 None（方向安全：不会把非分类文档误算成已分类）。"""
    kl = key.lower()
    i = kl.find('/md/images/')
    if i >= 0:
        return key[:i]
    i = kl.find('/md/')
    if i >= 0:
        return key[:i]
    for seg in SEGMENTS:
        i = kl.find(seg)
        if i >= 0:
            return key[:i]
    if kl.endswith('.md'):
        return key[:-3]
    return None


def dir_of_key(key):
    """对象 key → 所属 P20 顶目录（第一个 '/' 前那段 + '/'）。"""
    i = key.find('/')
    return key[:i + 1] if i >= 0 else key


def list_p20_dirs(s3, root=''):
    """列桶根下 P20* 顶层目录（13 个）；root 只做前缀过滤，不递归子目录。"""
    token = None
    out = []
    while True:
        params = {'Bucket': BUCKET, 'Delimiter': '/', 'MaxKeys': 1000}
        if token:
            params['ContinuationToken'] = token
        resp = retry_call(s3.list_objects_v2, **params)
        for cp in resp.get('CommonPrefixes', []):
            p = cp['Prefix']
            if p.startswith(MATCH) and (not root or p.startswith(root)):
                out.append(p)
        if not resp.get('IsTruncated'):
            return sorted(out)
        token = resp.get('NextContinuationToken')
        if not token:
            return sorted(out)


def _pdf_key(key):
    return key[:-4]


def export_dir(di, prefix, f_pdf, f_dirs, s3):
    """单目录导出：pass1 收 md（保留+兜底集合），pass2 分流 pdf/images。"""
    t0 = time.time()
    dpath = os.path.join(PARTS_DIR, 'd%02d' % di)
    os.makedirs(dpath, exist_ok=True)
    f_del = io.open(os.path.join(dpath, 'delete.tsv'), 'w', encoding='utf-8')
    f_keep = io.open(os.path.join(dpath, 'keep_md.tsv'), 'w', encoding='utf-8')
    f_unc = io.open(os.path.join(dpath, 'unclassified.tsv'), 'w', encoding='utf-8')
    st = {'prefix': prefix, 'keys': 0, 'md_keep_n': 0, 'md_keep_b': 0,
          'md_unc_n': 0, 'pdf_cls_n': 0, 'pdf_cls_b': 0,
          'img_cls_n': 0, 'img_cls_b': 0,
          'pdf_unc_n': 0, 'pdf_unc_b': 0, 'img_unc_n': 0, 'img_unc_b': 0,
          'other_n': 0, 'other_samples': []}
    md_docs = set()
    md_unc_samples = []
    nkeys = 0

    # pass1：所有 .md（A 型 + B/C 平铺），建 md_docs 兜底集合
    for key, size in iter_keys_with_size(s3, prefix):
        nkeys += 1
        kl = key.lower()
        if kl.endswith('.md'):
            d = doc_dir(key)
            if d is None:
                st['other_n'] += 1
                if len(st['other_samples']) < OTHER_SAMPLE_MAX:
                    st['other_samples'].append(key)
                continue
            if d in f_dirs:
                md_docs.add(d)
                st['md_keep_n'] += 1
                st['md_keep_b'] += size or 0
                f_keep.write('%s\t%d\n' % (key, size or 0))
            else:
                st['md_unc_n'] += 1
                if len(md_unc_samples) < 50:
                    md_unc_samples.append(key)
        elif kl.endswith('.pdf') or '/md/images/' in kl:
            continue   # pass2 处理
        elif any(seg in kl for seg in SEGMENTS):
            continue   # 产物 QA/CoT/Json/SFT 保留
        else:
            st['other_n'] += 1
            if len(st['other_samples']) < OTHER_SAMPLE_MAX:
                st['other_samples'].append(key)
        if nkeys % PROGRESS_EVERY == 0:
            print('[%2d] pass1 %s keys=%d md_keep=%d (%.0fs)' % (
                di, prefix[:40], nkeys, st['md_keep_n'], time.time() - t0),
                flush=True)
    st['keys'] = nkeys

    # pass2：pdf + MD/images 分流
    no_md = {}       # doc → [pdf_n, pdf_b, img_n, img_b]
    nkeys = 0
    for key, size in iter_keys_with_size(s3, prefix):
        nkeys += 1
        kl = key.lower()
        size = size or 0
        if kl.endswith('.pdf'):
            d = _pdf_key(key)
            cls = d in f_dirs
            if cls and d in md_docs:
                st['pdf_cls_n'] += 1
                st['pdf_cls_b'] += size
                f_del.write('%s\tpdf\t%s\t%d\n' % (BUCKET, key, size))
            elif cls:
                rec = no_md.setdefault(d, [0, 0, 0, 0])
                rec[0] += 1
                rec[1] += size
            else:
                st['pdf_unc_n'] += 1
                st['pdf_unc_b'] += size
                f_unc.write('%s\tpdf\t%s\t%d\n' % (BUCKET, key, size))
        elif '/md/images/' in kl:
            d = doc_dir(key)
            cls = bool(d) and d in f_dirs
            if cls and d in md_docs:
                st['img_cls_n'] += 1
                st['img_cls_b'] += size
                f_del.write('%s\timg\t%s\t%d\n' % (BUCKET, key, size))
            elif cls:
                rec = no_md.setdefault(d, [0, 0, 0, 0])
                rec[2] += 1
                rec[3] += size
            else:
                st['img_unc_n'] += 1
                st['img_unc_b'] += size
                f_unc.write('%s\timg\t%s\t%d\n' % (BUCKET, key, size))
        if nkeys % PROGRESS_EVERY == 0:
            print('[%2d] pass2 %s keys=%d pdf=%d img=%d (%.0fs)' % (
                di, prefix[:40], nkeys, st['pdf_cls_n'], st['img_cls_n'],
                time.time() - t0), flush=True)
    f_del.close()
    f_keep.close()
    f_unc.close()

    with io.open(os.path.join(dpath, 'no_md.tsv'), 'w', encoding='utf-8') as fh:
        for d in sorted(no_md):
            pn, pb, inn, ib = no_md[d]
            fh.write('%s\t%d\t%d\t%d\t%d\n' % (d, pn, pb, inn, ib))
    st['docs_cls_n'] = len({f for f in f_pdf if f.startswith(prefix)})
    st['no_md_docs'] = len(no_md)
    st['no_md_pdf_n'] = sum(v[0] for v in no_md.values())
    st['no_md_img_n'] = sum(v[2] for v in no_md.values())
    st['docs_del_n'] = len(md_docs)
    st['md_unc_samples'] = md_unc_samples
    st['elapsed_sec'] = round(time.time() - t0, 1)
    line = ('[%2d] %-46s keys=%d 文档=%d 待删pdf=%d/%.2fGB 待删img=%d/%.2fGB '
            '留md=%d 缺md文档=%d 未分类pdf=%d/img=%d other=%d 用时%.0fs') % (
        di, prefix[:46], st['keys'], st['docs_del_n'], st['pdf_cls_n'],
        st['pdf_cls_b'] / 2 ** 30, st['img_cls_n'], st['img_cls_b'] / 2 ** 30,
        st['md_keep_n'], st['no_md_docs'], st['pdf_unc_n'], st['img_unc_n'],
        st['other_n'], st['elapsed_sec'])
    with _lock:
        print(line, flush=True)
    return st


def merge_parts(dirs_count):
    """合并各目录 part 文件为最终清单，并清理临时目录。"""
    counts = {'del': 0, 'keep': 0, 'unc': 0, 'no_md': 0}
    sizes = {'del': 0}
    for out_name, part_name, cnt_key in (
            (DELETE_LIST, 'delete.tsv', 'del'),
            (KEEP_MD, 'keep_md.tsv', 'keep'),
            (UNCLASSIFIED, 'unclassified.tsv', 'unc'),
            (NO_MD, 'no_md.tsv', 'no_md')):
        with io.open(out_name, 'w', encoding='utf-8') as out:
            for di in range(dirs_count):
                p = os.path.join(PARTS_DIR, 'd%02d' % di, part_name)
                if not os.path.exists(p):
                    continue
                with io.open(p, encoding='utf-8') as fh:
                    for ln in fh:
                        if not ln.strip():
                            continue
                        out.write(ln if ln.endswith('\n') else ln + '\n')
                        counts[cnt_key] += 1
                        if cnt_key == 'del':
                            try:
                                sizes['del'] += int(ln.rsplit('\t', 1)[1])
                            except (IndexError, ValueError):
                                pass
    return counts, sizes


def cmd_export(args):
    s3 = get_s3(args.workers)
    t_start = time.time()
    dirs = list_p20_dirs(s3, args.root)
    print('P20 目录发现: %d 个' % len(dirs), flush=True)
    for d in dirs:
        print('   ', d)
    if not dirs:
        print('未发现 P20 目录，退出', flush=True)
        return 1
    if args.limit_dirs:
        dirs = dirs[:args.limit_dirs]
        print('（--limit-dirs=%d：只扫 %s）' % (args.limit_dirs, dirs[0]), flush=True)

    committed, led = load_committed(s3, args.ledger_workers,
                                    refresh=args.refresh_ledger,
                                    max_age_min=args.ledger_max_age)
    f_pdf = {f for f in committed if f.lower().endswith('.pdf')}
    f_dirs = {f[:-4] for f in f_pdf}
    print('committed(p20)=%d → pdf=%d doc_dir=%d（非 pdf committed=%d）' % (
        len(committed), len(f_pdf), len(f_dirs), len(committed) - len(f_pdf)),
        flush=True)

    if os.path.isdir(PARTS_DIR):
        for n in os.listdir(PARTS_DIR):
            p = os.path.join(PARTS_DIR, n)
            if os.path.isdir(p):
                for f in os.listdir(p):
                    os.remove(os.path.join(p, f))
                os.rmdir(p)
    os.makedirs(PARTS_DIR, exist_ok=True)

    stats = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(export_dir, i, p, f_pdf, f_dirs, s3): p
                for i, p in enumerate(dirs)}
        for i, fut in enumerate(as_completed(futs), 1):
            st = fut.result()
            stats.append(st)
            print('  [%d/%d] 完成 %s（累计 %.0fs）' % (
                i, len(dirs), st['prefix'][:40], time.time() - t0), flush=True)

    counts, sizes = merge_parts(len(dirs))
    other_samples = []
    for st in stats:
        for k in st['other_samples']:
            if len(other_samples) < OTHER_SAMPLE_MAX:
                other_samples.append(k)
        st.pop('other_samples', None)
    with io.open(OTHER_KEYS, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(other_samples) + ('\n' if other_samples else ''))

    total = {
        'keys': sum(s['keys'] for s in stats),
        'docs_cls_n': sum(s['docs_cls_n'] for s in stats),
        'docs_del_n': sum(s['docs_del_n'] for s in stats),
        'del_pdf_n': sum(s['pdf_cls_n'] for s in stats),
        'del_pdf_b': sum(s['pdf_cls_b'] for s in stats),
        'del_img_n': sum(s['img_cls_n'] for s in stats),
        'del_img_b': sum(s['img_cls_b'] for s in stats),
        'md_keep_n': sum(s['md_keep_n'] for s in stats),
        'md_keep_b': sum(s['md_keep_b'] for s in stats),
        'md_unc_n': sum(s['md_unc_n'] for s in stats),
        'no_md_docs': sum(s['no_md_docs'] for s in stats),
        'no_md_pdf_n': sum(s['no_md_pdf_n'] for s in stats),
        'no_md_img_n': sum(s['no_md_img_n'] for s in stats),
        'pdf_unc_n': sum(s['pdf_unc_n'] for s in stats),
        'pdf_unc_b': sum(s['pdf_unc_b'] for s in stats),
        'img_unc_n': sum(s['img_unc_n'] for s in stats),
        'img_unc_b': sum(s['img_unc_b'] for s in stats),
        'other_n': sum(s['other_n'] for s in stats),
        'delete_list_lines': counts['del'],
        'delete_list_b': sizes['del'],
    }
    res = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
           'bucket': BUCKET, 'prefix': 'P20*', 'scanned_dirs': len(dirs),
           'criteria': '分类 = dd organize_records committed=true 的 f（按 f 去重）；'
                       'P20 前缀 = f 以 P20 开头；删 = 已分类文档的 pdf + MD/images，'
                       '保留 md（含 B/C 型平铺 md）；缺 md 的文档不删',
           'ledger': led, 'elapsed_sec': round(time.time() - t_start, 1),
           'total': total, 'dirs': sorted(stats, key=lambda x: x['prefix'])}
    with io.open(EXPORT_STATS, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    print('\n== 导出汇总 ==', flush=True)
    print('扫描目录 %d | keys %d | 已分类文档 %d | 可删文档 %d' % (
        len(dirs), total['keys'], total['docs_cls_n'], total['docs_del_n']))
    print('待删 PDF  %d 个 / %.2f GB' % (total['del_pdf_n'], total['del_pdf_b'] / 2 ** 30))
    print('待删 Images %d 个 / %.2f GB' % (total['del_img_n'], total['del_img_b'] / 2 ** 30))
    print('删除合计 %.2f GB（清单 %d 行，大小校验 %.2f GB）' % (
        (total['del_pdf_b'] + total['del_img_b']) / 2 ** 30,
        counts['del'], sizes['del'] / 2 ** 30))
    print('保留 MD %d 个 / %.2f GB' % (total['md_keep_n'], total['md_keep_b'] / 2 ** 30))
    print('逐 key 清单: %s' % DELETE_LIST)
    print('缺 MD 文档(不删): %d 篇 / pdf %d / img %d -> %s' % (
        total['no_md_docs'], total['no_md_pdf_n'], total['no_md_img_n'], NO_MD))
    print('分类外 pdf/img(不删): %d/%d 个，%.2f GB -> %s' % (
        total['pdf_unc_n'], total['img_unc_n'],
        (total['pdf_unc_b'] + total['img_unc_b']) / 2 ** 30, UNCLASSIFIED))
    print('未归类 other: %d（样例 %s）' % (total['other_n'], OTHER_KEYS))
    print('未分类平铺 md: %d（样例见 stats json）' % total['md_unc_n'])
    print('耗时 %.0fs -> %s' % (time.time() - t_start, EXPORT_STATS), flush=True)
    if total['no_md_docs'] or total['pdf_unc_n'] or total['img_unc_n']:
        print('⚠️ 存在缺 MD 文档或分类外对象：核对清单后再执行 delete --yes', flush=True)
    return 0


def valid_delete_key(kind, key):
    if not key or not key.startswith(MATCH):
        return False
    kl = key.lower()
    if kl.endswith('.md'):
        return False
    if kind == 'pdf':
        return kl.endswith('.pdf')
    if kind == 'img':
        return '/md/images/' in kl
    return False


def read_list():
    rows = []
    if not os.path.exists(DELETE_LIST):
        return rows
    with io.open(DELETE_LIST, encoding='utf-8-sig') as fh:
        for ln in fh:
            ln = ln.rstrip('\n').rstrip('\r')
            if not ln:
                continue
            parts = ln.split('\t')
            if len(parts) < 4:
                continue
            bucket, kind, key, size = parts[0], parts[1], parts[2], parts[3]
            try:
                size = int(size)
            except ValueError:
                size = 0
            rows.append((bucket, kind, key, size))
    return rows


def cmd_delete(args):
    rows = read_list()
    if not rows:
        print('清单不存在或为空：%s（先跑 export）' % DELETE_LIST, flush=True)
        return 1
    todo, bad = [], []
    for bucket, kind, key, size in rows:
        if bucket != BUCKET:
            bad.append((bucket, kind, key, '桶不允许'))
        elif not valid_delete_key(kind, key):
            bad.append((bucket, kind, key, '类型/前缀校验失败'))
        else:
            todo.append((bucket, kind, key, size))
    print('清单 %d 行，合法 %d，非法 %d' % (len(rows), len(todo), len(bad)), flush=True)
    for b in bad[:20]:
        print('  非法: %s' % (b,))
    if args.fresh and os.path.exists(DELETE_CKPT):
        os.remove(DELETE_CKPT)
    fp = '%s|%s' % (todo[0][2] if todo else '', todo[-1][2] if todo else '')
    offset = 0
    if args.resume and os.path.exists(DELETE_CKPT):
        try:
            with io.open(DELETE_CKPT, encoding='utf-8') as fh:
                ck = json.load(fh)
            if ck.get('total') != len(todo) or ck.get('fp') != fp:
                print('断点与当前清单不一致（total %s→%d，fp %s），需 --fresh 重头删' % (
                    ck.get('total'), len(todo), '变了' if ck.get('fp') != fp else '未变'),
                    flush=True)
                return 2
            offset = int(ck.get('offset', 0))
            print('断点续删：已完成 %d / %d 行' % (offset, len(todo)), flush=True)
        except Exception as e:  # noqa: BLE001
            print('断点读取失败，忽略: %s' % e, flush=True)

    pdf_n = sum(1 for r in todo[offset:] if r[1] == 'pdf')
    img_n = sum(1 for r in todo[offset:] if r[1] == 'img')
    total_b = sum(r[3] for r in todo[offset:])
    print('%s：pdf %d 个，img %d 个，合计 %.2f GB，起点 %d' % (
        '真删' if args.yes else 'dry-run', pdf_n, img_n, total_b / 2 ** 30,
        offset), flush=True)

    if not args.yes:
        by_dir = Counter(dir_of_key(r[2]) for r in todo[offset:])
        for d, n in by_dir.most_common(20):
            print('   %-50s %d' % (d[:50], n))
        print('dry-run 完成：未删除任何对象。确认后加 --yes --resume', flush=True)
        return 0

    s3 = get_s3(DELETE_WORKERS)
    errf = io.open(DELETE_ERRORS, 'a', encoding='utf-8')
    n_obj = n_err = 0
    t0 = time.time()

    def do_batch(batch):
        keys = [r[2] for r in batch]
        try:
            resp = s3.delete_objects(Bucket=BUCKET, Delete={
                'Objects': [{'Key': k} for k in keys], 'Quiet': True})
        except Exception as e:  # noqa: BLE001
            return 0, ['exception\t%s\t%s' % (batch[0][2] if batch else '', e)]
        errs = []
        for e in resp.get('Errors', []):
            errs.append('%s\t%s\t%s' % (e.get('Key'), e.get('Code'), e.get('Message')))
        return len(keys) - len(errs), errs

    with ThreadPoolExecutor(max_workers=MAX_INFLIGHT) as ex:
        pos = offset
        while pos < len(todo):
            chunk = todo[pos:pos + MAX_INFLIGHT * BATCH]
            batches = [chunk[i:i + BATCH] for i in range(0, len(chunk), BATCH)]
            futs = [ex.submit(do_batch, b) for b in batches]
            for fut in futs:
                ok_n, errs = fut.result()
                n_obj += ok_n
                n_err += len(errs)
                for line in errs:
                    errf.write(line + '\n')
            pos += len(chunk)
            with io.open(DELETE_CKPT, 'w', encoding='utf-8') as fh:
                json.dump({'offset': pos, 'total': len(todo), 'fp': fp,
                           'generated_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                          fh, ensure_ascii=False)
            errf.flush()
            print('  进度 %d/%d 对象 %d 错误 %d (%.0fs)' % (
                pos, len(todo), n_obj, n_err, time.time() - t0), flush=True)
    errf.close()
    remain = Counter(dir_of_key(r[2]) for r in todo[offset:])
    print('删除完成：对象 %d，错误 %d；涉及 %d 个目录，用时 %.0fs' % (
        n_obj, n_err, len(remain), time.time() - t0), flush=True)
    if n_err:
        print('失败明细见 %s（可人工核对后重删）' % DELETE_ERRORS, flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description='P20 已分类 PDF + MD/images 清理')
    sub = ap.add_subparsers(dest='cmd')
    pe = sub.add_parser('export', help='导出待删清单（只读）')
    pe.add_argument('--workers', type=int, default=WORKERS)
    pe.add_argument('--limit-dirs', type=int, default=0)
    pe.add_argument('--root', default='', help='只扫以该前缀开头的 P20 目录')
    pe.add_argument('--ledger-workers', type=int, default=LEDGER_WORKERS)
    pe.add_argument('--refresh-ledger', action='store_true')
    pe.add_argument('--ledger-max-age', type=int, default=120)

    pd = sub.add_parser('delete', help='按清单删除（默认 dry-run）')
    pd.add_argument('--yes', action='store_true', help='真正删除')
    pd.add_argument('--resume', action='store_true', help='按断点续删')
    pd.add_argument('--fresh', action='store_true', help='忽略断点重头删')

    args = ap.parse_args()
    if args.cmd == 'export':
        return cmd_export(args)
    if args.cmd == 'delete':
        return cmd_delete(args)
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
