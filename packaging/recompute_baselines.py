"""修正后的基线重算。

更正：上一版诊断脚本里
    '总选最后一个候选': pick(lambda r: -0)
`-0 == 0`，与 '总选第一个候选' 是同一个排序键——那一行是重复项，
不是独立基线。最后一个候选恒为 wrongperiod（合成项），标签全负，实际 0%。

本脚本逐条显式取值，不再用排序技巧，避免同类错误。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
H = ROOT / '答辩评测' / 'external_programmatic_holdout_all.jsonl'


def main() -> int:
    rows = [json.loads(l) for l in H.read_text(encoding='utf-8').splitlines() if l.strip()]
    by_claim: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_claim[r['claim_id']].append(r)
    # 按文件中的原始顺序保留
    claims = list(by_claim.values())
    n = len(claims)
    print(f'claim {n} · 候选 {len(rows)}')
    print()

    def rate(fn, name):
        ok = sum(1 for c in claims if (p := fn(c)) is not None and p['label'] == 1)
        print(f'  {name:26} {ok:>4}/{n}  {ok/n*100:>7.2f}%')
        return ok / n

    print('=== 修正后的基线（逐条显式取值）===')
    rate(lambda c: c[0], '总选第 1 个候选')
    rate(lambda c: c[-1], '总选最后 1 个候选')
    rate(lambda c: next((x for x in c if x['fact_id'].endswith('-current')), None), '总选 -current')
    rate(lambda c: next((x for x in c if x['fact_id'].endswith('-prior')), None), '总选 -prior')
    rate(lambda c: next((x for x in c if x['fact_id'].endswith('-wrongperiod')), None), '总选 -wrongperiod')
    rate(lambda c: next((x for x in c if '本期' in (x.get('fact_text') or '')), None), '总选 fact_text 含本期')
    rate(lambda c: next((x for x in c if '上期' in (x.get('fact_text') or '')), None), '总选 fact_text 含上期')
    # 固定选第 2 个（prior 位置）
    rate(lambda c: c[1] if len(c) > 1 else None, '总选第 2 个候选(prior 位置)')
    print()

    # 随机期望：按每个 claim 的正例比例算
    exp = sum(sum(1 for x in c if x['label'] == 1) / len(c) for c in claims) / n
    print(f'=== 随机选一个候选的期望 Top-1: {exp*100:.2f}% ===')
    print()

    print('=== 位置 × 标签 交叉表（验证"位置即答案"）===')
    pos_label = Counter()
    for c in claims:
        for i, x in enumerate(c):
            pos_label[(i, x['label'])] += 1
    for i in range(3):
        p1 = pos_label.get((i, 1), 0)
        p0 = pos_label.get((i, 0), 0)
        print(f'  位置 {i+1}: 正例 {p1:>4} · 负例 {p0:>4}  → 该位置为正例的概率 {p1/(p1+p0)*100:>6.2f}%')
    print()

    print('=== 结论 ===')
    print(f'  位置 1 为正例概率 100%，位置 3 为 0% → 候选顺序完全决定答案。')
    print(f'  零信息基线: 选位置1 = 100%、选 position3 = 0%、随机 = {exp*100:.1f}%')
    print(f'  模型 63.89% 高于随机 {exp*100:.1f}%，但**低于**"固定选第 1 个"的 100% ——')
    print(f'  说明模型没有利用构造顺序，但也没有超过它；该基准无法区分语义能力。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
