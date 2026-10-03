"""验证假设：零产出公司的目标页里，表格为何抽不出来。

对比 pdfplumber 的两种取表方式：
  extract_tables()  —— 基于线条/边框
  find_tables()     —— 同一算法，但返回对象
并检查页面文本是否可用"文本行"方式解析出 (指标, 本期值, 上期值)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / '答辩评测' / 'annual_reports_all'
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
SEC2 = re.compile(r'第\s*二\s*节\s*公司简介和主要财务指标|主要会计数据和财务指标')
SEC3 = re.compile(r'第\s*三\s*节\s*(?:管理层讨论与分析|经营情况讨论与分析)|管理层讨论与分析|经营情况讨论与分析')
TOC = re.compile(r'目\s*录')


def main() -> int:
    import pymupdf
    import pdfplumber

    manifest = [json.loads(l) for l in (D / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    cands = [json.loads(l) for l in (D / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    have = {c['company'] for c in cands}
    zero = [r for r in manifest if r['stock_code'] not in have]

    print(f'零产出公司 {len(zero)} 家，抽 20 家核查\n')
    import statistics
    rows = []
    for rec in zero[:20]:
        p = D / rec['local_file']
        if not p.is_file():
            continue
        try:
            doc = pymupdf.open(p)
            pages = [(i + 1, (pg.get_text('text') or '')) for i, pg in enumerate(doc)]
            doc.close()
        except Exception:
            continue
        target = set()
        for idx, (pno, txt) in enumerate(pages):
            head = txt[:600]
            if TOC.search(head) and idx < 8:
                continue
            if SEC2.search(head):
                target.update(range(pno, min(len(pages), pno + 4) + 1))
            if SEC3.search(head):
                target.update(range(pno, min(len(pages), pno + 30) + 1))
        n_tables = n_pages_with_textrows = 0
        for pno in sorted(target):
            try:
                with pdfplumber.open(p) as pdf:
                    if pno > len(pdf.pages):
                        continue
                    tabs = pdf.pages[pno - 1].extract_tables() or []
                    n_tables += len(tabs)
                    text = pages[pno - 1][1]
            except Exception:
                continue
            # 文本行方式：一行里出现「中文名 + 至少两个数值」
            for line in text.splitlines():
                s = line.strip()
                if len(s) < 6 or len(s) > 200:
                    continue
                nums = [m.group(0) for m in NUM.finditer(s)]
                zh = len(re.findall(r'[\u4e00-\u9fff]', s))
                if len(nums) >= 2 and zh >= 3:
                    n_pages_with_textrows += 1
                    break
        rows.append({'code': rec['stock_code'], 'target_pages': len(target),
                     'tables': n_tables, 'pages_with_textrows': n_pages_with_textrows})
        print(f"  {rec['stock_code']}  目标页 {len(target):>3}  抽到表 {n_tables:>2}  "
              f"文本行可解析页 {n_pages_with_textrows:>3}")

    if rows:
        print()
        print(f"  合计：目标页 {sum(r['target_pages'] for r in rows)} · "
              f"抽到表 {sum(r['tables'] for r in rows)} · "
              f"文本行可解析页 {sum(r['pages_with_textrows'] for r in rows)}")
        print(f"  → 若文本行方式可解析，潜在新增事实页 "
              f"{sum(r['pages_with_textrows'] for r in rows)} 页")

    # 挑一家看具体文本形态
    if rows:
        rec = next(r for r in zero if r['stock_code'] == rows[0]['code'])
        p = D / rec['local_file']
        doc = pymupdf.open(p)
        pages = [(i + 1, (pg.get_text('text') or '')) for i, pg in enumerate(doc)]
        doc.close()
        for idx, (pno, txt) in enumerate(pages):
            if SEC2.search(txt[:600]) and not (TOC.search(txt[:600]) and idx < 8):
                print(f'\n=== {rec["stock_code"]} 第 {pno} 页（第二节起始）原文前 1200 字 ===')
                print(txt[:1200])
                break
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
