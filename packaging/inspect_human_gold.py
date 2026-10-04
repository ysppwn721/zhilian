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
n = len(qs)

print(f'题目 {n} · 候选 {len(rows)}')
print()
print('=== 题干模板（去数字后）===')
pat = Counter(re.sub(r'\d+', '#', q[0]['claim_text']) for q in qs)
for k, v in pat.most_common(8):
    print(f'  {v:>4}  {k[:70]}')
print()
print('=== task_type × claim_variant ===')
print(' ', dict(Counter((q[0]['task_type'], q[0].get('claim_variant')) for q in qs)))
print()
print('=== 每题候选数分布 ===')
print(' ', dict(sorted(Counter(len(q) for q in qs).items())))
print()
print('=== 每题正例数 / expected_k ===')
print('  正例数:', dict(sorted(Counter(sum(1 for r in q if r['label'] == 1) for q in qs).items())))
print('  expected_k:', dict(Counter(q[0].get('expected_k') for q in qs)))
print()
print('=== gold_fact_ids 与 fact_id 的对应 ===')
q = qs[0]
print(f"  gold_fact_ids = {q[0]['gold_fact_ids']}")
print('  该题全部 fact_id:')
for r in q:
    print(f"     {r['fact_id']:<28} label={r['label']}  period={r.get('period')} scope={r.get('scope')}")
print()

print('=== 零信息基线（逐条显式取值）===')
def rate(fn, name):
    ok = sum(1 for q in qs if (p := fn(q)) is not None and p.get('label') == 1)
    print(f'  {name:34} {ok:>5}/{n}  {ok/n*100:>7.2f}%')
    return ok / n

rate(lambda q: q[0], '总选第 1 个候选')
rate(lambda q: q[-1], '总选最后 1 个候选')
rate(lambda q: q[len(q) // 2], '总选中位候选')
exp = sum(sum(1 for r in q if r['label'] == 1) / len(q) for q in qs) / n
print(f'  {"随机选一个的期望":34} {"":>5}    {exp*100:>7.2f}%')
print()
print('  各位置正例率:')
pos = Counter()
tot = Counter()
for q in qs:
    for i, r in enumerate(q):
        pos[i] += (r['label'] == 1)
        tot[i] += 1
for i in sorted(tot):
    if tot[i] >= 20:
        print(f'    位置 {i:>2}: {pos[i]:>4}/{tot[i]:>4} = {pos[i]/tot[i]*100:>6.2f}%')
