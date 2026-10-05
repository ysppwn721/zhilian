"""查清两项质量检查失败的成因。"""
import json
from collections import defaultdict
from pathlib import Path

DEST = Path('答辩评测/v3_eval_20261005/v3_human_gold_40/final_api_route_comparison')
GOLD = Path('答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl')

rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
groups = defaultdict(list)
for r in rows:
    groups[r['claim_id']].append(r)

route = json.loads((DEST / 'three_route_result.json').read_text(encoding='utf-8'))
print('=== A) 三层路由：增长题预测期间组合非法的条目 ===')
for x in route['per_claim']:
    if x['task_type'] != 'growth_set':
        continue
    preds = [r for r in groups[x['claim_id']] if r['fact_id'] in x['pred_fact_ids']]
    if not preds:
        continue
    periods = sorted(set(r['period'] for r in preds))
    if periods not in (['上期', '本期'], ['本期'], ['上期']):
        print(f"  {x['claim_id']} route={x['route']} 期间={periods} match={x['exact_match']}")
        for r in preds:
            print(f"      {r['fact_id']:<44} period={r['period']} label={r['label']}")
        print(f"      gold={x['gold_fact_ids']}")
        print(f"      claim=「{groups[x['claim_id']][0]['claim_text'][:88]}」")
print()
# 也看看是否有"两条同为某期间"的情况
dup = 0
for x in route['per_claim']:
    if x['task_type'] != 'growth_set':
        continue
    preds = [r for r in groups[x['claim_id']] if r['fact_id'] in x['pred_fact_ids']]
    ps = [r['period'] for r in preds]
    if len(ps) > 1 and len(set(ps)) < len(ps):
        dup += 1
print(f'  预测中两条同为同一期间的题数: {dup}')
print()
print('=== B) 纯 API 顺序稳定性：8 条抽样中哪 2 条不一致 ===')
pure = json.loads((DEST / 'pure_api_result.json').read_text(encoding='utf-8'))
print('  （不一致题目的原预测，供人工复核；重跑结果未落盘，仅记录数量）')
ids = sorted(groups)[:8]
for cid in ids:
    x = [p for p in pure['per_claim'] if p['claim_id'] == cid][0]
    print(f"  {cid} [{x['task_type']}] 原预测={x['pred_fact_ids']} match={x['exact_match']}")
