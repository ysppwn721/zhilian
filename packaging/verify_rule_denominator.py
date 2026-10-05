"""复核「纯规则单值 Top-1 = 94.7%」的分母，消除评委会质疑的歧义。

出现歧义的原因
--------------
报告里有两个都在说"规则"的数字：
  A. 「纯规则（唯一才用）」单值 Top-1 = 94.7%  —— 来自 当前模型与自动路由比较.json
     （该文件是"规则+年报BERT"离线路由的整体结果，不是规则单独的成绩）
  B. 三层路由 by_route['rules'] 只处理 3 条，准确率 1.0

两者分母不同：A 是整批单值题，B 是规则实际接手的子集。
本脚本把 6 种可能的分母全部算出来，明确每个数字的含义。
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'packaging'))
sys.path.insert(0, str(ROOT / '答辩评测'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

G = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / 'human_gold.jsonl'
DEST = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / 'final_api_route_comparison'
CMP = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / '当前模型与自动路由比较.json'

rows = [json.loads(l) for l in G.read_text(encoding='utf-8').splitlines() if l.strip()]
groups: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    groups[r['claim_id']].append(r)
for cid in groups:
    groups[cid].sort(key=lambda r: r['candidate_position'])

import build_current_route_comparison as rb   # unique_rule / evidence

quote_ids = [cid for cid, g in groups.items() if g[0]['task_type'] == 'quote_current']
growth_ids = [cid for cid, g in groups.items() if g[0]['task_type'] == 'growth_set']

rule_refs = {}
for cid in quote_ids:
    refs = rb.unique_rule(groups[cid])
    rule_refs[cid] = refs

answered = {cid: r for cid, r in rule_refs.items() if r}
correct = {cid: r for cid, r in answered.items()
           if set(r) == {x['fact_id'] for x in groups[cid] if x['label'] == 1}}

print(f'单值题总数 {len(quote_ids)}')
print(f'规则给出唯一答案（可回答） {len(answered)}')
print(f'其中答对 {len(correct)}')
print()
print('=== 各种分母下的"规则单值准确率" ===')
n = len(quote_ids)
a = len(answered)
c = len(correct)
print(f'  1) 分母=全部单值题 ({n})              : {c}/{n} = {c/n*100:.1f}%   ← 含未回答算错')
print(f'  2) 分母=规则可回答题 ({a})            : {c}/{a} = {c/a*100:.1f}%   ← 可回答样本准确率' if a else '  2) n/a')
print(f'  3) 覆盖率（可回答/全部）              : {a}/{n} = {a/n*100:.1f}%')
print(f'  4) 拒答/转交他层比例                  : {n-a}/{n} = {(n-a)/n*100:.1f}%')
print()
print('=== 与三层路由 by_route 对照 ===')
route = json.loads((DEST / 'three_route_result.json').read_text(encoding='utf-8'))
print(f"  路由统计 rules 处理 {route['route_counts']['rules']} 条，准确率 "
      f"{route['metrics']['by_route'].get('rules', {}).get('accuracy')}")
print('  说明：路由里 rules 只接手"唯一确定"的题，其余 34 条转给年报 BERT / API，')
print('        因此 rules 的分母是 3，而不是 19。')
print()
print('=== 「纯规则（唯一才用）」这个标签的由来 ===')
cmp = json.loads(CMP.read_text(encoding='utf-8'))
tr = cmp['models']['three_route_offline']
print(f"  当前模型与自动路由比较.json 的 three_route_offline:")
print(f"    quote_top1        = {tr['quote_top1']}  （= {round(tr['quote_top1']*19)}/19）")
print(f"    growth_exact      = {tr['growth_exact']}")
print(f"    route_counts      = {tr['route_counts']}")
print(f"    api_fallback      = {tr['api_fallback']}")
print()
print('  → 该数字是**离线三层路由**在全部 19 条单值题上的成绩，')
print('    绝不是"规则单独处理 3 条"的成绩。我在报告里把它标成')
print('    「纯规则（唯一才用）」是**标注错误**，会被评委质疑分母。')
print()
print('=== 正确标注 ===')
print(f"  规则层（唯一才用）: 接手 {a}/19 条，准确率 {c/a*100:.1f}%（可回答样本），"
      f"覆盖率 {a/n*100:.1f}%" if a else '')
print(f"  三层路由整体      : 单值 {route['metrics']['quote_top1']*100:.1f}%"
      f"（分母 19，含规则 3 + 年报BERT 31 中的单值部分 + API 兜底）")
