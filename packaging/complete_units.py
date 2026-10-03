"""补齐 unit 字段 + 排查同表单位混用。

两类问题
--------
A. unit 为空（8 条）：不能默认填「元」——其中 4 条根本不是货币
   （每股收益=元/股、研发占比=%、吞吐量=万TEU），填错就是量纲错误。
B. 同 caption 内单位混用（元 vs 万元，差 10⁴）：必须逐条回原表核对，
   不能按组统一填。

做法：对每条空 unit 的候选，回原 PDF 该页取「单位：X」声明；
     若无声明，则按指标名中的量纲标记推断（元／股、%、TEU 等）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
UNIT_DECL = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')

# 指标名里自带量纲——这类绝不能填成货币单位
NAME_UNIT_RULES = [
    (re.compile(r'每股收益|每股净资产|每股'), '元/股'),
    (re.compile(r'比例\s*[（(]%|占比|率\s*[（(]%|（%）|\(%\)'), '%'),
    (re.compile(r'万TEU|标准箱'), '万TEU'),
    (re.compile(r'万千瓦|装机容量'), '万千瓦'),
    (re.compile(r'万千瓦时|电量'), '万千瓦时'),
    (re.compile(r'万元'), '万元'),
    (re.compile(r'人数|数量|个数|人数（人）'), '人'),
]


def infer_from_name(metric: str) -> tuple[str, str] | None:
    for pat, unit in NAME_UNIT_RULES:
        if pat.search(metric or ''):
            return unit, f'指标名含量纲标记 → {unit}'
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    args = ap.parse_args()
    d = args.corpus

    import pymupdf

    tc = [json.loads(l) for l in (d / 'training_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]

    # ---- A. 先按指标名量纲强制校正（优先级最高）----
    # 根因：单位声明是"页面级"的——页面写「单位：元」，抽取器就整页套用，
    # 但同页「主要财务指标」表里的每股收益（元／股）、收益率（%）本就不是元。
    # 实测错误：加权平均净资产收益率 4.0 被标成「元」；基本每股收益 0.44 被标成「元」。
    # 这类量纲错误比 unit 为空危险得多，因此指标名自带量纲标记时一律以指标名为准。
    corrected, filled, unresolved = [], 0, []
    for r in tc:
        metric = r['metric'] or ''
        guess = infer_from_name(metric)
        if guess and r.get('unit') != guess[0]:
            corrected.append((r, r.get('unit'), guess[0], guess[1]))
            r['unit'] = guess[0]
            r['unit_source'] = f'name_override: {guess[1]}'
            filled += 1
            continue
        if guess:
            r['unit_source'] = f'name_inference: {guess[1]}'
            continue
        if r.get('unit'):
            r['unit_source'] = 'extracted'
            continue
        # 名称无线索且为空 → 回原页找单位声明
        p = d / r['source_file']
        decl = None
        if p.is_file():
            doc = pymupdf.open(p)
            try:
                pno = r.get('fact_page') or 1
                if 1 <= pno <= len(doc):
                    m = UNIT_DECL.search(doc[pno - 1].get_text('text') or '')
                    if m:
                        decl = m.group(2)
            finally:
                doc.close()
        if decl:
            r['unit'], r['unit_source'] = decl, f'page_declaration: 第{r["fact_page"]}页「单位：{decl}」'
            filled += 1
        else:
            r['unit_source'] = 'unresolved'
            unresolved.append(r)

    # ---- A2. 两条推导规则（用于 A 之后仍为空的少数条目）----
    # (1) 同公司同指标在别页已有单位 → 取之。
    #     实测：600512 营业收入第 16 页 caption 空、无单位声明，
    #     但同公司第 14 页同一指标同一数值声明「元」。
    known = {}
    for r in tc:
        if r.get('unit') and r.get('unit_source') != 'unresolved':
            known.setdefault((r['company'], r['metric']), set()).add(r['unit'])
    # (2) 同一张表内的子项行继承本表首个有单位行的单位。
    #     实测：001872「其中：内地」「海外」是「集装箱吞吐量（万TEU）」的子项，
    #     表内无单位声明，但同表首行给出了量纲。
    sibling = {}
    for r in tc:
        if r.get('unit'):
            sibling.setdefault((r['company'], r.get('fact_page')), r['unit'])

    derived = []
    still = []
    for r in unresolved:
        key = (r['company'], r['metric'])
        if key in known and len(known[key]) == 1:
            r['unit'] = next(iter(known[key]))
            r['unit_source'] = f'derived_same_metric_elsewhere: 同公司同指标在别页单位为{r["unit"]}'
            derived.append(r)
            continue
        skey = (r['company'], r.get('fact_page'))
        if skey in sibling:
            r['unit'] = sibling[skey]
            r['unit_source'] = f'derived_sibling_row: 同表其他行单位为{r["unit"]}'
            derived.append(r)
            continue
        # (3) 指标名跨行断开时，单位常被抽成**紧随其后的单独一行**。
        #     实测 002630 第 9 页：
        #       ['归属于上市公司股东的净利润', '-193,068,024.70', ...]
        #       ['（元）', None, None, None, None]      ← 单位在下一行
        #     因此不能只在同一单元格里找后缀，要往下找单位行。
        tail_unit = None
        p = d / r['source_file']
        if p.is_file():
            import pdfplumber
            try:
                with pdfplumber.open(p) as pdf:
                    pno = r.get('fact_page') or 1
                    if 1 <= pno <= len(pdf.pages):
                        base = re.sub(r'[\s\u3000]+', '', r['metric'])
                        for t in (pdf.pages[pno - 1].extract_tables() or []):
                            rows_norm = [[re.sub(r'[\s\u3000]+', '', str(c or '')) for c in row]
                                         for row in t]
                            for i, row in enumerate(rows_norm):
                                # 该行首个非空单元格正是本条指标
                                first = next((c for c in row if c), '')
                                if not first or base not in first:
                                    continue
                                # 同单元格后缀
                                m = re.search(r'[（(]([^）)]{1,8})[）)]', first)
                                if m and len(first) > len(base):
                                    tail_unit = m.group(1).replace('／', '/')
                                    break
                                # 向下最多 2 行找纯单位行
                                for nxt in rows_norm[i + 1:i + 3]:
                                    nf = next((c for c in nxt if c), '')
                                    m2 = re.fullmatch(r'[（(]([^）)]{1,8})[）)]', nf)
                                    if m2:
                                        tail_unit = m2.group(1).replace('／', '/')
                                        break
                                if tail_unit:
                                    break
                            if tail_unit:
                                break
            except Exception:
                tail_unit = None
        if tail_unit:
            r['unit'] = tail_unit
            r['unit_source'] = f'derived_metric_suffix: 原表该行名含「（{tail_unit}）」（PDF 换行导致后缀丢失）'
            derived.append(r)
            continue
        still.append(r)

    unresolved = still

    # ---- B. 同 caption 单位混用排查 ----
    by_cap = defaultdict(list)
    for r in tc:
        by_cap[(r.get('caption') or '').strip()[:40]].append(r)
    mixed = {cap: rows for cap, rows in by_cap.items()
             if len({row.get('unit') for row in rows}) > 1}

    (d / 'training_candidates.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in tc), encoding='utf-8')

    print(f'可训练候选 {len(tc)} 条')
    print(f'  量纲校正/补齐   : {filled} 条')
    print(f'  按规则推导      : {len(derived)} 条')
    print(f'  仍无法确定      : {len(unresolved)} 条')
    print()
    print(f'  单位分布: {dict(Counter(r.get("unit") or "（空）" for r in tc))}')
    print(f'  来源分布: {dict(Counter(str(r.get("unit_source","")).split(":")[0] for r in tc))}')
    print()
    if corrected:
        print('=== 量纲校正明细（这些原先是错的）===')
        for r, old, new, why in corrected:
            print(f"  {r['company']} {r['metric'][:26]:26} {str(old):>6} → {new:8} ({why})")
        print()
    if derived:
        print('=== 按规则推导明细 ===')
        for r in derived:
            print(f"  {r['company']} {r['metric'][:24]:24} → {r['unit']:8} ({r['unit_source'][:56]})")
        print()
    if unresolved:
        print('=== 仍需人工确认单位 ===')
        for r in unresolved:
            print(f"  {r['company']} {r['metric'][:24]:24} 值={r['value']} 表页={r['fact_page']}")
    print()
    print('=== 同 caption 内单位混用（须逐条回表核对）===')
    if not mixed:
        print('  无')
    for cap, rows in mixed.items():
        print(f"  「{cap}」")
        for r in rows:
            print(f"      {r['company']} {r['metric'][:24]:24} 值={r['value']:>18} 单位={r.get('unit')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
