"""诊断：gold_fact_ids 是否真的存在于该题候选集合内。"""
import json
import pathlib
from collections import Counter, defaultdict

G = pathlib.Path('答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl')
rows = [json.loads(l) for l in G.read_text(encoding='utf-8').splitlines() if l.strip()]
groups = defaultdict(list)
for r in rows:
    groups[r['claim_id']].append(r)

print(f'论断 {len(groups)} · 候选 {len(rows)}')
print()
missing = []
for cid, g in groups.items():
    allowed = {r['fact_id'] for r in g}
    gold = set(g[0]['gold_fact_ids'])
    bad = gold - allowed
    if bad:
        missing.append((cid, sorted(gold), sorted(bad), sorted(allowed)[:4]))
print(f'gold_fact_ids 不在候选集合内的题: {len(missing)} / {len(groups)}')
for cid, gold, bad, allowed in missing[:8]:
    print(f'  {cid}')
    print(f'     gold   : {gold}')
    print(f'     缺失   : {bad}')
    print(f'     候选样例: {allowed}')
print()
# 指标名后缀问题
suf = Counter()
for cid, g in groups.items():
    for r in g:
        m = r['metric']
        if '（元）' in m or '(元)' in m or '（%）' in m or '(%)' in m:
            suf['带后缀指标名'] += 1
        else:
            suf['不带后缀'] += 1
print(f'候选指标名: {dict(suf)}')
print()
gold_suf = Counter()
for cid, g in groups.items():
    for fid in g[0]['gold_fact_ids']:
        gold_suf['带（元）' if ('（元）' in fid or '(元)' in fid) else '不带'] += 1
print(f'gold_fact_ids: {dict(gold_suf)}')
