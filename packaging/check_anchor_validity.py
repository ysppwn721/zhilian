"""核查 751 条"claim 文本不含本期值"的候选：是容差误报，还是真错配。"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / '答辩评测' / 'annual_reports_final'
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')


def to_num(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').replace('－', '-').strip('()')
    try:
        return float(x.rstrip('%'))
    except ValueError:
        return None


def main() -> int:
    tc = [json.loads(l) for l in (D / 'training_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]

    exact = near = far = none = 0
    samples = {'near': [], 'far': [], 'none': []}
    for r in tc:
        s = r.get('claim_text') or ''
        v = to_num(r.get('value'))
        pri = to_num(r.get('fact_prior_value'))
        nums = [to_num(m.group(0)) for m in NUM.finditer(s)]
        nums = [n for n in nums if n is not None]
        if not nums:
            none += 1
            if len(samples['none']) < 5:
                samples['none'].append(r)
            continue
        targets = [t for t in (v, pri) if t is not None]
        if not targets:
            none += 1
            continue
        best = min((min(abs(n - t) / max(abs(t), 1e-9) for t in targets) for n in nums), default=1e9)
        if best <= 1e-9:
            exact += 1
        elif best <= 0.01:
            near += 1
            if len(samples['near']) < 5:
                samples['near'].append((r, best))
        else:
            far += 1
            if len(samples['far']) < 5:
                samples['far'].append((r, best))

    print(f'总计 {len(tc)} 条')
    print(f'  精确命中（值字面出现在文本）: {exact}')
    print(f'  容差内（≤1%，多为单位/取整差）: {near}')
    print(f'  差距 >1%（疑似错配）        : {far}')
    print(f'  文本无任何数字             : {none}')
    print()

    if samples['near']:
        print('=== 容差内样例（看是否是单位换算差）===')
        for r, d in samples['near']:
            print(f"  {r['company']} {r['metric'][:20]} 值={r['value']} 单位={r.get('unit')} 偏差={d*100:.3f}%")
            print(f"     「{(r['claim_text'] or '')[:96]}」")
        print()
    if samples['far']:
        print('=== 差距 >1% 样例（这些可能是真错配）===')
        for r, d in samples['far']:
            print(f"  {r['company']} {r['metric'][:20]} 值={r['value']} 上期={r['fact_prior_value']} 偏差={d*100:.1f}%")
            print(f"     「{(r['claim_text'] or '')[:110]}」")
        print()
    if samples['none']:
        print('=== 文本无数字样例 ===')
        for r in samples['none']:
            print(f"  {r['company']} {r['metric'][:20]} 值={r['value']}")
            print(f"     「{(r['claim_text'] or '')[:110]}」")
        print()

    # 按 claim_kind 分别看
    print('=== 按类型 ===')
    per = Counter()
    for r in tc:
        s = r.get('claim_text') or ''
        v = to_num(r.get('value'))
        nums = [to_num(m.group(0)) for m in NUM.finditer(s)]
        nums = [n for n in nums if n is not None]
        hit = v is not None and any(abs(n - v) <= max(abs(v), 1) * 0.01 for n in nums)
        per[(r.get('claim_kind'), 'hit' if hit else 'miss')] += 1
    for k, c in sorted(per.items()):
        print(f'  {k[0]:8} {k[1]:5} {c:>5}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
