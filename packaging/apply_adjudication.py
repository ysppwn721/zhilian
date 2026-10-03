"""第 1 步下半段：把人工填好的口径判定回填到候选记录。

用法
----
1. 先打开 adjudication_ledger.csv，填三列：
     口径判定(人工填)   ：合并 / 母公司 / 分部 / 排除
     确认单位(人工填)   ：元 / 万元 / 亿元 / 千元
     判定人、判定时间
2. 运行本脚本，它会：
     - 校验每一行都填了（未填的行会列出并阻止，除非 --allow-partial）
     - 按 caption 把判定回填到每条候选的 scope / unit
     - 重新计算哪些候选满足准入门槛，输出 adjudicated_candidates.jsonl
     - 对判定为「排除」的 caption，其候选标记为 rejected 并说明原因

判定口径的含义
--------------
  合并   ：合并报表口径（年报第二节"主要会计数据"的惯例）
  母公司 ：母公司报表口径
  分部   ：按业务/产品/地区拆分
  排除   ：该表不进入训练集（例如 caption 不可信、或表义不明）
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
VALID_SCOPE = {'合并', '母公司', '分部', '排除'}
VALID_UNIT = {'元', '万元', '亿元', '千元'}
# 准入门槛：三值自洽（或报告未给变动列），且口径与单位均已确认
ADMIT_CHECK = {'consistent', 'no_change_value', 'sign_convention_differs'}


def norm_caption(c: str) -> str:
    c = (c or '').strip()
    return c[:60] if len(c) >= 4 else '（空/无效 caption）'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    ap.add_argument('--allow-partial', action='store_true',
                    help='允许台账未填完（未填的 caption 视为"未判定"，其候选不进入训练集）')
    args = ap.parse_args()

    d = args.corpus
    ledger_path = d / 'adjudication_ledger.csv'
    if not ledger_path.is_file():
        print(f'✗ 找不到台账 {ledger_path}\n  先运行 packaging/make_adjudication_ledger.py')
        return 2

    with ledger_path.open(encoding='utf-8-sig', newline='') as fh:
        ledger = list(csv.DictReader(fh))

    # ---- 校验台账 ----
    unfilled, bad_scope, bad_unit = [], [], []
    decisions = {}
    for row in ledger:
        cap = row.get('表格标题(caption)', '')
        scope = (row.get('口径判定(人工填)') or '').strip()
        unit = (row.get('确认单位(人工填)') or '').strip()
        if not scope:
            unfilled.append(cap)
            continue
        if scope not in VALID_SCOPE:
            bad_scope.append((cap, scope))
            continue
        if unit and unit not in VALID_UNIT:
            bad_unit.append((cap, unit))
            continue
        decisions[norm_caption(cap)] = {'scope': scope, 'unit': unit or None,
                                       'by': (row.get('判定人') or '').strip(),
                                       'at': (row.get('判定时间') or '').strip()}

    if bad_scope:
        print('✗ 口径判定取值非法（只能是 合并/母公司/分部/排除）：')
        for cap, v in bad_scope:
            print(f'    「{cap[:40]}」= {v}')
        return 2
    if bad_unit:
        print('✗ 单位取值非法（只能是 元/万元/亿元/千元）：')
        for cap, v in bad_unit:
            print(f'    「{cap[:40]}」= {v}')
        return 2
    if unfilled:
        print(f'⚠ 台账有 {len(unfilled)}/{len(ledger)} 组未填口径判定：')
        for cap in unfilled[:12]:
            print(f'    「{cap[:46]}」')
        if not args.allow_partial:
            print('\n填完再运行，或加 --allow-partial（未判定的候选不进训练集）。')
            return 1
        print('  （--allow-partial：这些组的候选将标记为未经判定，不进训练集）')

    # ---- 回填 ----
    cs = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]

    admitted, pending, excluded = [], [], []
    for c in usable:
        key = norm_caption(c.get('caption'))
        dec = decisions.get(key)
        c2 = dict(c)
        if not dec:
            c2['scope_confirmed'] = None
            c2['unit_confirmed'] = None
            c2['admission'] = 'needs_scope_adjudication'
            pending.append(c2)
            continue
        if dec['scope'] == '排除':
            c2['scope_confirmed'] = '排除'
            c2['admission'] = 'excluded_by_adjudication'
            excluded.append(c2)
            continue
        c2['scope_confirmed'] = dec['scope']
        c2['unit_confirmed'] = dec['unit'] or c2.get('unit')
        c2['adjudicated_by'] = dec['by']
        c2['adjudicated_at'] = dec['at']
        if c2.get('three_value_check') in ADMIT_CHECK:
            c2['admission'] = 'admitted'
            admitted.append(c2)
        else:
            c2['admission'] = f"rejected_three_value_{c2.get('three_value_check')}"
            excluded.append(c2)

    out = d / 'adjudicated_candidates.jsonl'
    out.write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in admitted), encoding='utf-8')
    (d / 'adjudicated_pending.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in pending), encoding='utf-8')
    (d / 'adjudicated_excluded.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in excluded), encoding='utf-8')

    print()
    print('=== 回填结果 ===')
    print(f'  台账已判定 caption : {len(decisions)}/{len(ledger)} 组')
    print(f'  可进入训练         : {len(admitted)} 条')
    print(f'  待口径判定         : {len(pending)} 条')
    print(f'  排除               : {len(excluded)} 条')
    print()
    if admitted:
        by_scope = Counter(c['scope_confirmed'] for c in admitted)
        by_check = Counter(c['three_value_check'] for c in admitted)
        by_kind = Counter(c['claim_kind'] for c in admitted)
        print(f'  口径分布 : {dict(by_scope)}')
        print(f'  三值分布 : {dict(by_check)}')
        print(f'  类型分布 : {dict(by_kind)}')
        print(f'  单位分布 : {dict(Counter(c.get("unit_confirmed") for c in admitted))}')
        print(f'  公司数   : {len({c["company"] for c in admitted})}')
        print(f'  本期:上期来源 = {sum(1 for c in admitted if c["claim_kind"] in ("growth","quote"))}:'
              f'{sum(1 for c in admitted if c["claim_kind"] == "growth")}')
    print()
    print(f'  → adjudicated_candidates.jsonl / adjudicated_pending.jsonl / adjudicated_excluded.jsonl')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
