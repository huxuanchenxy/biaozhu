# pdf_rename_inflate_upload.py 使用说明

MinIO 里的 PDF 批量「膨胀」：取原件 → 在内存里追加填充（不改页面内容）→ 上传 `X_sssss.pdf`
备份原件 → 用膨胀版覆盖 `X.pdf`。上传前可选校验产物能不能被阅读器打开。

---

## 0. 准备

- Python 3.8+（Windows 用 `python` 或 `py -3`；Linux 用 `python3`）
- 可选依赖：`pip install pymupdf`
  装了校验走 PyMuPDF 内核口径；不装自动降级为「尾部指针判定」，**不会误拦**，只是记录粗一点。
- **只需要这一个文件**：连接信息（endpoint / AK / SK / 目录前缀）都写在文件顶部 `CONFIG` 里，
  同级目录不需要 `.env`，也不需要 `pdf_check_lib.py`。
- Linux 上建议 `chmod 600 pdf_rename_inflate_upload.py`（文件内有明文 AK/SK）。
- 默认并发 4 线程（`-w`）。瓶颈是上行带宽，`-w 8` 通常就够，再大没收益。

---

## 1. 先干跑（不改桶，只看计划）

```bash
python pdf_rename_inflate_upload.py -n 0
```

看最后那段统计里的 `计划：新建 N，补做 M，重做 0，跳过 K`。数字符合预期再加 `--apply`。

> `-n 0` = 不限。**全目录模式默认 `-n 10`**，不写只做 10 个就停。

---

## 2. 全量跑（前台）

在脚本所在目录执行。

**Windows cmd：**

```bat
cd /d G:\project\biaozhu\src\workers
chcp 65001
python pdf_rename_inflate_upload.py -n 0 -w 8 --apply
```

**Windows PowerShell：**

```powershell
cd G:\project\biaozhu\src\workers
python pdf_rename_inflate_upload.py -n 0 -w 8 --apply
```

**Linux：**

```bash
cd /opt/pdfwork
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
python3 pdf_rename_inflate_upload.py -n 0 -w 8 --apply
```

默认行为（不加 `--refix`）：**只处理还没膨胀过的**（没有 `_sssss` 的新文件，外加极少量的「有备份但上次没覆盖完」的补做），
已完成的跳过。中断后重新执行同一条命令即可继续，不会重复做已完成的部分。

---

## 3. 后台跑

运行时间按小时算，建议后台。

**Windows cmd（最小化窗口，输出进文件）：**

```bat
cd /d G:\project\biaozhu\src\workers
start "pdf膨胀" /min cmd /c "python -u pdf_rename_inflate_upload.py -n 0 -w 8 --apply > run_20261030.out 2>&1"
```

不要关那个最小化窗口（关掉等于结束进程）；关掉当前 cmd 窗口不影响它。

**Windows PowerShell（隐藏窗口）：**

```powershell
Start-Process -WindowStyle Hidden -FilePath python -ArgumentList `
  "pdf_rename_inflate_upload.py","-n","0","-w","8","--apply" `
  -RedirectStandardOutput "run_20261030.out" -RedirectStandardError "run_20261030.err"
```

**Linux（nohup）：**

```bash
cd /opt/pdfwork
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nohup python3 -u pdf_rename_inflate_upload.py -n 0 -w 8 --apply > run_20261030.out 2>&1 &
echo $!
```

---

## 4. 看进度

后台跑时输出不是终端，脚本会自动切成「每 5 秒一行」的心跳（不会把文件刷成乱码长行）：

```
列目录中：drivdernet_abc/自筹/…/成品语料 …（对象多要翻很多页；每 5s 报一行进度，没输出不等于卡住）
  [5.1s] 列目录中，已发现 PDF 12000
  [18.2s] 目录共 45309 个 PDF（不含子目录）
  [23.6s] 扫描中，已探测 3000，待处理 812，已处理完 5
[1/xxxx] ✓ 00D84E8D….pdf：新建 膨胀 2.0MB→7.0MB 校验 0.0s …
```

- `已发现 PDF`：列目录进度（含 `_sssss`，约为源文件的 2 倍）
- `已探测` / `待处理` / `已处理完`：HEAD 预筛与本轮实际完成数
- 心跳间隔可用环境变量 `PDF_HEARTBEAT`（秒）调整，如 `set PDF_HEARTBEAT=10`

**Windows cmd 查看：**

```bat
type run_20261030.out
powershell -c "Get-Content run_20261030.out -Tail 20 -Wait"
```

**Linux 查看：**

```bash
tail -f run_20261030.out
```

脚本自己还会写一份流水日志 `pdf_processed_<目录尾段>_<时间戳>.log`（**固定落在脚本所在目录**，与当前工作目录无关）。

文件名里的“目录尾段”取自 `--prefix` 的最后两段（如 `5.1_通用材料基因数据_成品语料`），所以同一台机器上两个不同目录并行跑时，日志自动分开、不会打架。

- 逐文件一行 TSV，最后一列是校验结论：`校验：源件正常/产物正常`、`打不开(短因)`
- 失败/被拦下的行状态是 `FAILED` / `BLOCKED`，这份日志可直接当名单重跑
- 末尾有一段 `# 开头` 的汇总块（人数、耗时、数据量），窗口关了也能查

---

## 5. 停止

**Windows：**

```bat
tasklist /fi "imagename eq python.exe"
taskkill /f /pid 12345
```

**Linux：**

```bash
kill -INT <pid>     # 等价 Ctrl+C：在途的做完、收尾、把汇总块写进日志
kill <pid>          # SIGTERM 强杀：不写汇总块
```

注意：

- Windows 的 `taskkill /f` 是强杀，同样**不写汇总块**；想留汇总就让前台窗口里 Ctrl+C 或等它跑完。
- 千万别 `taskkill /f /im python.exe`，会把机器上所有 python 进程一起结束。

---

## 6. 两个目录同时跑（两个窗口）

同一个脚本、两个 cmd 窗口，**只差 `--prefix`**，日志名会自动带上目录尾段，两份互不覆盖：

```bat
:: 窗口 1：自筹（用 CONFIG 里的默认前缀）
cd /d G:\project\biaozhu\src\workers
python pdf_rename_inflate_upload.py -n 0 -w 4 --apply
:: 日志：pdf_processed_5.1_通用材料基因数据_成品语料_20261030xxxxxx.log

:: 窗口 2：国拨（只多一个 --prefix）
cd /d G:\project\biaozhu\src\workers
python pdf_rename_inflate_upload.py --prefix "drivdernet_abc/国拨/5_工业标准基础数据/5.2_机械工程与制造/成品语料" -n 0 -w 4 --apply
:: 日志：pdf_processed_5.2_机械工程与制造_成品语料_20261030xxxxxx.log
```

要点：

- `-w 4` 而不是 8：两个进程合计保持 8 线程，总吞吐不变但更少撞 429；并行不会让总时间变短（瓶颈仍是上行带宽）
- 两个目录不重叠，不需要 `--shard`
- 前缀不要写开头的 `/`（写成 `/drivdernet_abc/…` 时脚本会自动去掉，但别依赖这个）
- 想固定日志名就用 `--log 我的名字.log`，此时不会再插目录尾段

---

## 7. 单文件验证 / 定点点名

名单模式：配了 `--files` 就不再扫全目录，只处理点名的文件（写成 `X.pdf` 或 `X_sssss.pdf` 都归一到同一份）。

```bash
# 干跑看它打算怎么处理
python pdf_rename_inflate_upload.py --files "bad.pdf" -n 0

# 真正执行
python pdf_rename_inflate_upload.py --files "bad.pdf" -n 0 --apply
```

原件超过尺寸上限（默认 5MB）的，必须临时放宽上限，否则会被判「规则外」直接跳过：

```bash
python pdf_rename_inflate_upload.py --files "bad.pdf" --max-mb 12 -n 0 --apply
```

用日志文件当名单批量重试（每行一个名字，`#` 开头为注释）：

```bash
python pdf_rename_inflate_upload.py --files-from refix_pending.txt -n 0 --apply
```

---

## 8. 常用参数

| 参数 | 作用 |
|---|---|
| `--apply` | 真正执行（不加=干跑，不动桶） |
| `-n 0` | 不限个数（全目录默认 10） |
| `-w 8` | 并发线程数（默认 4） |
| `--files "A.pdf,B.pdf"` | 定向名单，逗号分隔 |
| `--files-from 文件` | 从文本读名单（相对路径按当前工作目录解析） |
| `--refix` | 重做已完成的：从 `_sssss` 取原件重新膨胀覆盖 `X.pdf` |
| `--refix-only` | 只动有 `_sssss` 备份的，不新建 |
| `--max-mb 12` | 放宽原件尺寸上限（只为单文件验证；放宽后已膨胀的完成版会被当原件再膨一次） |
| `--verify tail` / `off` | 强制零依赖口径 / 关闭上传前校验 |
| `--verify-strict` | 连「可疑」也拦（书签树、需重建 xref 这类实测能打开的也拦） |
| `--list-cap 5000` | 列目录只收这么多个就停（多机分片时各机须一致） |
| `--shards N --shard i` | 多机分片，各机处理互不相交的子集 |
| `--log` / `--log-ts off` | 只写参数不带值=不写日志 / 日志用固定文件名逐轮追加；不传 `--log` 时默认名自动带「目录尾段+时间戳」 |
| `-q` | 静默（输出重定向到文件时自动生效，不用手写） |

---

## 9. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 只做了 10 个就停 | 忘了 `-n 0`（全目录模式默认 10） |
| 单文件跑不动，报「拒绝处理」或「不像原件，跳过」 | 原件 ≥5MB，加 `--max-mb 12` |
| Windows 上执行 `python3` 弹应用商店 | 用 `python` 或 `py -3` |
| 控制台中文乱码 | `chcp 65001`（日志文件本身始终是 UTF-8） |
| 前缀多写了开头的 `/` | 会自动去掉；但别写成 `//` 或多层斜杠 |
| 后台跑看着没反应 | 前 10~60 秒在列目录（4 万个对象要翻几十页），每 5 秒会有一行心跳；没有输出才是异常 |
| `kill` 后日志没有汇总块 | `kill`（SIGTERM）/`taskkill /f` 都是强杀；用 `kill -INT` 或 Ctrl+C |
| 本机和服务器同时跑同一批 | 会各做一遍、重复上传，日志也混；只留一端 |
| 想复现「被拦下」 | 只有「源件正常 + 产物打不开」才拦；源件本身坏（如 bad.pdf）按规则照传不背锅，用 `--verify-strict` 才能看到可疑件被拦 |
