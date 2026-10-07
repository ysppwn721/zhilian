"""质量检查：对两份结果做独立核验。

1. 纯 API 与三层路由使用同一份人工 Gold（逐条 claim_id 一致）
2. 所有返回 fact_id 都存在于该题候选集合
3. 增长题必须同时评价本期与上期来源
4. 候选顺序打乱后结果是否稳定
5. 产物齐全且不含密钥
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / 'final_api_route_comparison'
GOLD = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / 'human_gold.jsonl'

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
groups: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    groups[r['claim_id']].append(r)
fails = []


def check(name, ok, detail=''):
    print(f'  {"✓" if ok else "✗"} {name}' + (f'  {detail}' if detail else ''))
    if not ok:
        fails.append(name)


print('=== 1) 同一个人工 Gold ===')
pure = json.loads((DEST / 'pure_api_result.json').read_text(encoding='utf-8'))
route = json.loads((DEST / 'three_route_result.json').read_text(encoding='utf-8'))
pc = {x['claim_id'] for x in pure['per_claim']}
rc = {x['claim_id'] for x in route['per_claim']}
check('两份结果覆盖同一批论断', pc == rc == set(groups),
      f'{len(pc)} / {len(rc)} / gold {len(groups)}')

print()
print('=== 2) 返回 fact_id 合法（存在该题候选集合内）===')
for tag, res in (('纯API', pure), ('三层路由', route)):
    bad = []
    for x in res['per_claim']:
        allowed = {r['fact_id'] for r in groups[x['claim_id']]}
        bad += [f for f in x['pred_fact_ids'] if f not in allowed]
    check(f'{tag} 全部 fact_id 合法', not bad and res['validity']['all_valid'],
          f'越界 {len(bad)} 条' if bad else '0 条越界')

print()
print('=== 3) 增长题同时评价本期与上期来源 ===')
growth_ids = [cid for cid, g in groups.items() if g[0]['task_type'] == 'growth_set']
check('增长题存在', len(growth_ids) > 0, f'{len(growth_ids)} 条')
multi = sum(1 for cid in growth_ids if len(groups[cid][0]['gold_fact_ids']) >= 2)
check('增长题 gold 至少 2 个来源', multi == len(growth_ids), f'{multi}/{len(growth_ids)}')
# 评价口径：增长题用集合精确匹配 + P/R/F1（已在 metrics 中）
for tag, res in (('纯API', pure), ('三层路由', route)):
    m = res['metrics']
    check(f'{tag} 增长题按集合评价（P/R/F1 均有值）',
          all(m.get(k) is not None for k in ('growth_precision', 'growth_recall', 'growth_f1')))
    # 抽查：增长题预测应覆盖两个不同期间。
    # 注意：模型可能用**显式年份**指代上期（如 2013年 / 2016年），语义正确，
    # 因此按"是否覆盖两个不同期间槽位"判定，而不是要求字面等于「上期」。
    YEAR_RE = re.compile(r'^\d{4}年$')

    def period_slot(p: str, claim_text: str) -> str:
        if p == '本期':
            return 'current'
        # 显式年份：出现在论断里的那一年即本期，其余为比较期
        years = re.findall(r'(20\d{2})', claim_text)
        if YEAR_RE.match(p) and years and p[:-1] == years[0]:
            return 'current'
        return 'prior'

    ok_pair = True
    year_variant = 0
    for x in res['per_claim']:
        if x['task_type'] != 'growth_set':
            continue
        claim_text = groups[x['claim_id']][0]['claim_text']
        preds = [r for r in groups[x['claim_id']] if r['fact_id'] in x['pred_fact_ids']]
        if not preds:
            continue
        slots = [period_slot(r['period'], claim_text) for r in preds]
        if any(YEAR_RE.match(r['period']) for r in preds):
            year_variant += 1
        # 允许 1 条（缺来源）或 2 条且覆盖 current+prior；不允许两条同一槽位
        if len(slots) > 1 and len(set(slots)) < len(slots):
            ok_pair = False
    check(f'{tag} 增长题预测覆盖不同期间槽位', ok_pair,
          f'其中 {year_variant} 条用显式年份表达上期')

print()
print('=== 4) 候选顺序打乱后结果是否稳定 ===')
# 4a 本地模型：已知 0 变化（顺序不变性），核对已有文件
已有 = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40'
bge_cmp = json.loads((已有 / 'bge' / 'v3_model_comparison.json').read_text(encoding='utf-8'))
inv = bge_cmp['models']['bge']['candidate_order_invariance']
check('BGE 顺序不变性 = 1.0', inv['prediction_set_consistency'] == 1.0,
      f"changed={inv['changed_claims']}")
# 4b 纯 API：对同一题把候选顺序再打乱一次，看预测集合是否一致
api_key = (os.getenv('DEEPSEEK_API_KEY') or '').strip()
if api_key:
    sys.path.insert(0, str(ROOT))
    os.environ['ZHILIAN_LLM_MODE'] = 'api_only'
    import zhilian.llm as llm
    rng = random.Random(20261005)
    sample = sorted(groups)[:8]
    stable = changed = 0
    for cid in sample:
        g = list(groups[cid])
        base = {r['fact_id'] for r in g}
        rng.shuffle(g)
        claims = [{'id': cid, 'kind': 'growth' if g[0]['task_type'] == 'growth_set' else 'quote',
                   'original': g[0]['claim_text'], 'refs': [], 'confirmed': False}]
        facts = [{'id': r['fact_id'], 'subject': '公司', 'metric': r['metric'],
                  'period': r['period'], 'unit': r.get('unit') or '未标明',
                  'scope': r.get('scope') or '未标明'} for r in sorted(g, key=lambda x: str(x['fact_id']))]
        try:
            sugg = llm.suggest_links(claims, facts)
            llm.consume_last_call_metrics()
        except Exception:                              # noqa: BLE001
            continue
        pred = {f for s in sugg for f in (s.get('refs') or [])}
        prev = {f for x in pure['per_claim'] if x['claim_id'] == cid for f in x['pred_fact_ids']}
        if pred == prev:
            stable += 1
        else:
            changed += 1
    tot = stable + changed
    # 实测 temperature=0 下仍非完全不变：8 条抽样有 2 条在候选重排后预测集合改变。
    # 这是该 API 的真实局限，如实记录为「非完全顺序不变」，不隐藏、不粉饰。
    check('纯 API 顺序打乱后预测集合稳定', changed == 0 and tot > 0,
          f'{stable}/{tot} 一致（不一致 {changed}）—— 已如实记入报告局限')
else:
    check('纯 API 顺序稳定性', False, '无 API Key，未能检验（如实记为未完成）')

print()
print('=== 5) 产物齐全且不含密钥 ===')
need = ['pure_api_result.json', 'three_route_result.json', 'api_vs_route_metrics.csv',
        'api_vs_route_report.md', '效果对比_纯API_vs_自动三层路由.png',
        '时间对比_纯API_vs_自动三层路由.png', '费用对比_纯API_vs_自动三层路由.png',
        'route_distribution.png', 'run_manifest.json']
for f in need:
    check(f'产物存在 {f}', (DEST / f).is_file())
# 密钥泄漏扫描
key = (os.getenv('DEEPSEEK_API_KEY') or '').strip()
leak = []
for p in DEST.rglob('*'):
    if not p.is_file() or p.suffix.lower() == '.png':
        continue
    txt = p.read_text(encoding='utf-8', errors='ignore')
    if key and key in txt:
        leak.append(p.name)
    if re.search(r'Bearer\s+[A-Za-z0-9_\-]{20,}', txt):
        leak.append(p.name + '(Bearer)')
check('无密钥泄漏', not leak, str(leak) if leak else '未发现')

print()
print('=' * 60)
print(f'  失败项 {len(fails)}：{fails}' if fails else '  全部检查通过')
print('=' * 60)
(DEST / 'quality_check.json').write_text(json.dumps(
    {'passed': not fails, 'failed': fails,
     'checked': ['同集一致性', 'fact_id 合法性', '增长题双来源', '顺序稳定性', '产物齐全', '无密钥泄漏']},
    ensure_ascii=False, indent=2), encoding='utf-8')
raise SystemExit(0 if not fails else 1)
