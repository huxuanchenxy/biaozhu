// 用前端同款 pdf.js 内核实测 PDF 能不能读（只解析，不画到屏幕）。
// 由 pdf_check_lib.py --pdfjs 批量调用：一次启动跑完所有文件，输出 TSV 供 Python 侧合并。
// 输出每行：  RESULT<TAB>OK|FAIL<TAB>耗时ms<TAB>页数<TAB>详情
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";
import { readFileSync } from "node:fs";

const files = process.argv.slice(2);
for (const f of files) {
  const t0 = Date.now();
  try {
    const data = new Uint8Array(readFileSync(f));
    const doc = await getDocument({ data, verbosity: 0 }).promise;
    const pages = doc.numPages;
    let words = 0;
    for (let i = 1; i <= Math.min(pages, 3); i++) {   // 走一遍页面字典+内容流，光看 numPages 不够
      const page = await doc.getPage(i);
      const tc = await page.getTextContent();
      words += tc.items.length;
      page.cleanup();
    }
    console.log(`RESULT\tOK\t${Date.now() - t0}\t${pages}\t取到词条 ${words}`);
    await doc.destroy();
  } catch (e) {
    console.log(`RESULT\tFAIL\t${Date.now() - t0}\t0\t${(e?.name || "Error")}: ${String(e?.message).replace(/\s+/g, " ").slice(0, 160)}`);
  }
}
