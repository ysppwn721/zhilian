"""填写「真实年报 Top-1 人工复核」表：只写 gold_refs / review_status / review_note。

复核依据
--------
《皖能电力》2018 年年度报告（000543_2018_皖能电力）。
12 条候选事实来自该报告「主要会计数据」表：6 个指标 × 本期/上期。
单值题给 1 个 fact_id；增长题给 2 个（按说明要求 prior, current 顺序）。
万元与元按 1 万元 = 10,000 元换算；四舍五入到论断所述精度后比对。

逐条判定
--------
1) 48d94f30a20205a650 quote 「经营活动产生的现金流量净额达13.67 亿元」 reported=13.67
   13.67 亿元 = 13,670,000,000 元 ≈ f3_current 1,367,348,095.07 元（1,367,348,095.07/1e8=13.6735→13.67）
   → confirmed, f3_current
2) 15568a60a229b167f1 growth 「经营活动产生的现金流量净额同比增加38.19%」 reported=38.19
   (f3_current - f3_prior)/|f3_prior| = (1367348095.07-989442166.46)/989442166.46 = 38.19%
   → confirmed, [f3_prior, f3_current]
3) 3f6a48a96ea0367f21 quote 「导致经营活动产生的现金流量净额13.67亿元」 reported=13.67
   与第 1 条同源事实 → confirmed, f3_current
4) 8fd6e1d7a6b952ca64 quote 「报告期内公司经营活动产生的现金流量净额为1,367,348,095.07元」
   reported=1367348095.07 精确匹配 f3_current（原句见报告段 494）
   → confirmed, f3_current
5) 0842785a0032193bf0 growth 「同比增长9.90%」 reported=9.9
   原文段 286：「实现主营业务收入1,341,645.69万元，同比增长9.90%」→ 指标为营业收入
   (f0_current - f0_prior)/|f0_prior| = (13416456919.36-12207433397.76)/12207433397.76 = 9.90%
   → confirmed, [f0_prior, f0_current]
6) 135c07b908539cf0d6 quote 「实现主营业务收入1,341,645.69万元」 reported=1341645.69
   1,341,645.69 万元 = 13,416,456,900 元 ≈ f0_current 13,416,456,919.36 元（1,341,645.691936→1,341,645.69）
   → confirmed, f0_current

6 条全部 confirmed，无需 reject。

用法：python packaging/fill_real_annual_review.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
REV = ROOT / 'output' / 'final_real_annual_verified_20261006' / 'real_annual_top1_review'
XLSX = REV / 'real_annual_top1_review.xlsx'
JSONL = REV / 'real_annual_top1_review.jsonl'

# 事实表（来自 000543_2018_皖能电力_事实源.xlsx，元）
FACTS = {
    'f0_current': ('营业收入', '本期', Decimal('13416456919.36')),
    'f0_prior': ('营业收入', '上期', Decimal('12207433397.76')),
    'f1_current': ('归属于上市公司股东的净利润', '本期', Decimal('556267729.59')),
    'f1_prior': ('归属于上市公司股东的净利润', '上期', Decimal('132054306.12')),
    'f2_current': ('归属于上市公司股东的扣除非经常性损益的净利润', '本期', Decimal('369022176.26')),
    'f2_prior': ('归属于上市公司股东的扣除非经常性损益的净利润', '上期', Decimal('122111479.81')),
    'f3_current': ('经营活动产生的现金流量净额', '本期', Decimal('1367348095.07')),
    'f3_prior': ('经营活动产生的现金流量净额', '上期', Decimal('989442166.46')),
    'f4_current': ('总资产', '本期', Decimal('28899887221.01')),
    'f4_prior': ('总资产', '上期', Decimal('26547647317.01')),
    'f5_current': ('归属于上市公司股东的净资产', '本期', Decimal('9796870921.27')),
    'f5_prior': ('归属于上市公司股东的净资产', '上期', Decimal('10135849055.69')),
}

REVIEW = {
    '48d94f30a20205a650': (['f3_current'], 'confirmed',
                           '经营活动产生的现金流量净额本期 1,367,348,095.07 元 = 13.67 亿元，'
                           '与论断 13.67 亿元一致（按亿元四舍五入 13.6735→13.67）。'
                           '口径为公司整体（主要会计数据摘要），单位元，期间本期。'),
    '15568a60a229b167f1': (['f3_prior', 'f3_current'], 'confirmed',
                           '增长题双来源：上期 989,442,166.46 元、本期 1,367,348,095.07 元；'
                           '(本期-上期)/|上期| = 38.19%，与论断 38.19% 精确一致。'
                           '顺序按 prior,current。口径公司整体，单位元。'),
    '3f6a48a96ea0367f21': (['f3_current'], 'confirmed',
                           '与论断 1 同源事实：经营活动产生的现金流量净额本期 1,367,348,095.07 元 = 13.67 亿元。'
                           '原文段 475「…导致经营活动产生的现金流量净额13.67亿元」。口径公司整体，期间本期。'),
    '8fd6e1d7a6b952ca64': (['f3_current'], 'confirmed',
                           '论断给出精确值 1,367,348,095.07 元，与本期事实完全一致（原文段 494 原样引用）。'
                           '口径公司整体，单位元，期间本期；该句同时提到净利润，但论断指标为现金流量净额。'),
    '0842785a0032193bf0': (['f0_prior', 'f0_current'], 'confirmed',
                           '论断片段为「同比增长9.90%」，原文段 286 的先行语是「实现主营业务收入1,341,645.69万元」，'
                           '故指标为营业收入而非总资产（总资产同比为 8.86%）。'
                           '(13,416,456,919.36-12,207,433,397.76)/12,207,433,397.76 = 9.90%，与论断一致。'
                           '顺序按 prior,current；口径公司整体，单位元。'),
    '135c07b908539cf0d6': (['f0_current'], 'confirmed',
                           '主营业务收入 1,341,645.69 万元 = 13,416,456,900 元，'
                           '与营业收入本期事实 13,416,456,919.36 元一致（按万元四舍五入 1,341,645.691936→1,341,645.69，'
                           '差 19.36 元）。口径公司整体，期间本期。'),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    import openpyxl

    wb = openpyxl.load_workbook(XLSX)
    ws = wb['复核表']
    header = [c.value for c in ws[1]]
    try:
        col_gold = header.index('gold_refs（填写ID，增长题填两个）') + 1
        col_status = header.index('review_status（confirmed/reject）') + 1
        col_note = header.index('review_note') + 1
    except ValueError as exc:
        print(f'表头未找到目标列: {exc}', file=sys.stderr)
        return 2
    col_id = header.index('claim_id') + 1
    col_kind = header.index('类型') + 1

    print(f'复核表 {ws.max_row - 1} 条；目标列 gold={col_gold} status={col_status} note={col_note}')
    filled = 0
    for r in range(2, ws.max_row + 1):
        cid = str(ws.cell(r, col_id).value or '').strip()
        kind = str(ws.cell(r, col_kind).value or '').strip()
        if cid not in REVIEW:
            print(f'  [跳过] 第 {r} 行 claim_id={cid} 不在复核清单内')
            continue
        refs, status, note = REVIEW[cid]
        # 自检：refs 必须是该行候选之一；数量与类型匹配
        cands = set()
        for c in range(6, 30, 2):                       # 候选N fact_id 列
            v = ws.cell(r, c).value
            if v:
                cands.add(str(v).strip())
        bad = [f for f in refs if f not in cands]
        if bad:
            print(f'  ✗ {cid}: refs {bad} 不在该行候选中，跳过', file=sys.stderr)
            continue
        want = 1 if kind == 'quote' else 2
        if len(refs) != want:
            print(f'  ✗ {cid}: {kind} 需 {want} 个 ref，给 {len(refs)} 个，跳过', file=sys.stderr)
            continue
        if not args.dry_run:
            ws.cell(r, col_gold).value = ','.join(refs)
            ws.cell(r, col_status).value = status
            ws.cell(r, col_note).value = note
        filled += 1
        print(f'  ✓ {cid} [{kind}] → {refs} {status}')

    if not args.dry_run:
        wb.save(XLSX)
        # 同步 JSONL（只改这三列）
        rows = [json.loads(l) for l in JSONL.read_text(encoding='utf-8').splitlines() if l.strip()]
        for row in rows:
            cid = row['claim_id']
            if cid in REVIEW:
                refs, status, note = REVIEW[cid]
                row['gold_refs'] = refs
                row['review_status'] = status
                row['review_note'] = note
        JSONL.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')

    print()
    print(f'{"（dry-run，未写入）" if args.dry_run else "已写入"} {filled} 条')
    print(f'  → {XLSX.relative_to(ROOT)}')
    print(f'  → {JSONL.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
