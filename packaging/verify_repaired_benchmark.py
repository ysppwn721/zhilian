"""核验修复版基准：零信息基线是否已被消除。

修复版要点（从字段推断）：
  - candidate_role 记录候选角色（同主体同期间不同指标 / 本期 / 上期 …）
  - gold_fact_ids 给出完整正例集合（支撑增长率类集合评价）
  - task_type: quote_current / quote_prior / growth_set，本期与上期配平
  - candidate_position 记录候选位置，用于检验"位置即答案"是否消除

逐条显式取值，不使用排序技巧（上一轮曾因 -0 == 0 出错）。
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
B = ROOT / '答辩评测' / 'annual_benchmark_repaired_20261004_v2' / 'benchmark.jsonl'


def main() -> int:
    rows = [json.loads(l) for l in B.read_text(encoding='utf-8').splitlines() if l.strip()]
    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_q[r['claim_id']].append(r)
    qs = list(by_q.values())
    n = len(qs)
    print(f'题目 {n} · 候选 {len(rows)}（每题 {len(rows)/n:.1f} 个）')
    print()

    print('=== 1) 题目类型与正例数 ===')
    tt = Counter()
    for q in qs:
        tt[q[0]['task_type']] += 1
    for k, v in tt.most_common():
        print(f'  {k:16} {v:>5} 题')
    print()
    gold_dist = Counter(len({r['fact_id'] for r in q if r['label'] == 1}) for q in qs)
    print(f'  每题正例数分布: {dict(sorted(gold_dist.items()))}')
    print()

    print('=== 2) 位置 × 标签（"位置即答案"是否消除）===')
    pos_label = Counter()
    for q in qs:
        for i, r in enumerate(q):
            pos_label[(i, r['label'])] += 1
    maxpos = max((i for i, _ in pos_label), default=0)
    for i in range(maxpos + 1):
        p1 = pos_label.get((i, 1), 0)
        p0 = pos_label.get((i, 0), 0)
        tot = p1 + p0
        if tot == 0:
            continue
        print(f'  位置 {i:>2}: 正例 {p1:>4} · 负例 {p0:>4} → 正例率 {p1/tot*100:>6.2f}%')
    print()

    print('=== 3) 零信息基线（逐条显式取值）===')
    def rate(fn, name):
        ok = sum(1 for q in qs if (p := fn(q)) is not None and p.get('label') == 1)
        print(f'  {name:34} {ok:>5}/{n}  {ok/n*100:>7.2f}%')
        return ok / n

    rate(lambda q: q[0], '总选第 1 个候选')
    rate(lambda q: q[-1], '总选最后 1 个候选')
    rate(lambda q: q[len(q) // 2], '总选中位候选')
    rate(lambda q: next((r for r in q if r['fact_id'].endswith('-current')), None), '总选 fact_id 以 -current 结尾')
    rate(lambda q: next((r for r in q if r['fact_id'].endswith('-prior')), None), '总选 fact_id 以 -prior 结尾')
    rate(lambda q: next((r for r in q if '2023年' in (r.get('period') or '')), None), '总选 period=2023年')
    rate(lambda q: next((r for r in q if r['metric'] == q[0]['gold_metric']), None), '总选 metric 等于 gold_metric')
    print()
    exp = sum(sum(1 for r in q if r['label'] == 1) / len(q) for q in qs) / n
    print(f'  随机选一个候选的期望 Top-1: {exp*100:.2f}%')
    print()

    print('=== 4) 候选角色分布（干扰项是否多样）===')
    roles = Counter(r['candidate_role'] for r in rows)
    for k, v in roles.most_common(12):
        print(f'  {k:44} {v:>6}')
    print()

    print('=== 5) 本期/上期是否配平（GPT 要求②）===')
    tp = Counter(r['task_type'] for r in rows if r['label'] == 1)
    print(f'  正例按任务类型: {dict(tp)}')
    print()
    print('=== 6) 结论 ===')
    best = max(
        sum(1 for q in qs if q[0].get('label') == 1),
        sum(1 for q in qs if q[-1].get('label') == 1),
    ) / n
    print(f'  最强的"按位置"基线: {best*100:.2f}%')
    print(f'  随机期望: {exp*100:.2f}%')
    if best <= exp * 1.5:
        print('  ✓ 位置捷径已消除（位置基线与随机接近）')
    else:
        print(f'  ✗ 位置仍有信息量（{best*100:.2f}% vs 随机 {exp*100:.2f}%）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
