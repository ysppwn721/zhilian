import json
import pathlib
import re
from collections import Counter, defaultdict

D = pathlib.Path('答辩评测/human_gold_eval_20261004')
rows = [json.loads(l) for l in (D / 'human_gold_20261004.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
byq: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    byq[r['claim_id']].append(r)
qs = list(byq.values())

print('=== 1) 按 task_type 看"末尾候选"是否就是正例 ===')
for task in ('quote_current', 'quote_prior', 'growth_set'):
    sel = [q for q in qs if q[0]['task_type'] == task]
    last_gold = sum(1 for q in sel if q[-1]['label'] == 1)
    first_gold = sum(1 for q in sel if q[0]['label'] == 1)
    print(f'  {task:14} {len(sel):>3} 题 · 末位是正例 {last_gold:>3} ({last_gold/len(sel)*100:>5.1f}%)'
          f' · 首位是正例 {first_gold:>3} ({first_gold/len(sel)*100:>5.1f}%)')

print()
print('=== 2) 正例的 (period, scope) 组合分布 ===')
c = Counter()
for q in qs:
    for r in q:
        if r['label'] == 1:
            c[(r.get('period'), r.get('scope'))] += 1
for k, v in c.most_common():
    print(f'  period={k[0]:<6} scope={k[1]:<6} {v:>4}')

print()
print('=== 3) 非正例的位置分布（看是否按角色排序）===')
# 猜测排序规则：按 (metric匹配?, period匹配?, scope匹配?) 排
for task in ('quote_current', 'quote_prior'):
    print(f'  --- {task} ---')
    for q in [x for x in qs if x[0]['task_type'] == task][:3]:
        print(f"    题干: {q[0]['claim_text'][:56]}")
        for i, r in enumerate(q):
            mark = '★' if r['label'] == 1 else ' '
            print(f"      {mark}[{i:>2}] {r['fact_id']:<34} period={r.get('period'):<6} scope={r.get('scope')}")

print()
print('=== 4) 关键检验：正例是否恒为"metric 与 gold_metric 相同且 period 匹配"的最后一个 ===')
# gold_metric 不在字段里，用 gold 的 metric
tail_metric = tail_period = 0
for q in qs:
    gold = [r for r in q if r['label'] == 1]
    gm = {r['metric'] for r in gold}
    last = q[-1]
    if last['metric'] in gm:
        tail_metric += 1
    if last.get('period') in {r.get('period') for r in gold}:
        tail_period += 1
print(f'  末位候选 metric 与正例一致: {tail_metric}/{len(qs)} = {tail_metric/len(qs)*100:.1f}%')
print(f'  末位候选 period 与正例一致: {tail_period}/{len(qs)} = {tail_period/len(qs)*100:.1f}%')

print()
print('=== 5) 若按"末尾优先"排序，各模型的数字会怎样 ===')
print(f'  纯规则(52%): 低于末尾基线 70%')
print(f'  BERT v2 本期(96%): 高于 70% → 有增量')
print(f'  年报修正版 本期(68%): **低于末尾基线 70%** → 无增量')
print(f'  BGE 本期(78%): 略高于 70% (+8 点)')
