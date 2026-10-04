"""按 (metric, period, scope) 三元组评分，绕开 fact_id 重名的 Gold 缺陷。

发现：Gold 集内 fact_id 不唯一。例如某题 gold 与预测都是「本期+上期两条」，
但两条 gold 事实的 fact_id 完全相同（同指标同期间、不同口径），
set(fact_id) 相等判定因此失败 —— 92% 被低估。

本脚本用三元组作为身份，重算分数，并区分：
  真错（指标或期间选错） vs 口径歧义（指标期间都对、仅口径不同）
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
GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'


def triple(r) -> tuple:
    return (r['metric'], r['period'], r.get('scope') or '未标明')


def main() -> int:
    rows = [json.loads(l) for l in (GD / 'human_gold_20261004_shuffled.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    byq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byq[r['claim_id']].append(r)
    preds = {}
    for l in (GD / 'human_gold_pure_api_20261004.jsonl').read_text(encoding='utf-8').splitlines():
        if l.strip():
            o = json.loads(l)
            preds[o['claim_id']] = o
    qs = [sorted(v, key=lambda r: r['candidate_position']) for v in byq.values()]

    print('=== 1) fact_id 唯一性检查 ===')
    dup = sum(1 for q in qs if len({c['fact_id'] for c in q}) < len(q))
    print(f'  题内 fact_id 有重名的题: {dup}/{len(qs)}')
    # gold 侧重名
    gold_dup = 0
    for q in qs:
        g = [c for c in q if c['label'] == 1]
        if len({c['fact_id'] for c in g}) < len(g):
            gold_dup += 1
    print(f'  gold 侧 fact_id 重名的题: {gold_dup}/{len(qs)}  ← 这些题用 set(fact_id) 必然判错')
    print()

    # 建立 fact_id → 候选 的多映射
    print('=== 2) 按两种口径重算 ===')
    ok_id = ok_tri = 0
    by_task_id = defaultdict(lambda: [0, 0])
    by_task_tri = defaultdict(lambda: [0, 0])
    err_kind = Counter()
    details = []
    for q in qs:
        cid = q[0]['claim_id']
        p = preds[cid]
        task = q[0]['task_type']
        gold_ids = {c['fact_id'] for c in q if c['label'] == 1}
        pred_ids = set(p['pred_fact_ids'])
        # 口径 A：fact_id
        a = pred_ids == gold_ids
        ok_id += a
        by_task_id[task][0] += a
        by_task_id[task][1] += 1
        # 口径 B：三元组
        gold_t = {triple(c) for c in q if c['label'] == 1}
        pred_t = {triple(c) for c in q if c['fact_id'] in pred_ids}
        b = pred_t == gold_t
        ok_tri += b
        by_task_tri[task][0] += b
        by_task_tri[task][1] += 1
        if not b:
            gm = {(c['metric'], c['period']) for c in q if c['label'] == 1}
            pm = {(c['metric'], c['period']) for c in q if c['fact_id'] in pred_ids}
            if pm == gm:
                err_kind['仅口径不同'] += 1
            elif pm & gm:
                err_kind['部分对（缺或多）'] += 1
            else:
                err_kind['指标或期间选错'] += 1
            details.append((task, cid, q[0]['claim_text'][:52], sorted(gold_ids), sorted(pred_ids),
                            sorted(gold_t), sorted(pred_t)))

    print(f'  口径A set(fact_id)  : {ok_id}/150 = {ok_id/150*100:.2f}%')
    print(f'  口径B set(三元组)   : {ok_tri}/150 = {ok_tri/150*100:.2f}%')
    print()
    print(f'  {"任务":16} {"口径A":>10} {"口径B":>10}')
    for t in ('quote_current', 'quote_prior', 'growth_set'):
        a = by_task_id[t]
        b = by_task_tri[t]
        print(f'  {t:16} {a[0]/a[1]*100:>9.2f}% {b[0]/b[1]*100:>9.2f}%')
    print()

    print('=== 3) 口径B 下仍错的题，按错误类型 ===')
    for k, v in err_kind.most_common():
        print(f'  {k:18} {v}')
    print()
    for task, cid, claim, gi, pi, gt, pt in details:
        print(f'  [{task:<14}] {cid}  「{claim}」')
        print(f'      gold 三元组: {gt}')
        print(f'      预测三元组 : {pt}')
        if gi == pi:
            print(f'      （fact_id 完全一致，但三元组不一致 → 说明候选身份有歧义）')
        print()
    print('=== 4) 结论 ===')
    print(f'  纯 API 真实集合精确匹配 = {ok_tri/150*100:.2f}%（口径B）')
    print(f'  此前报告的 92.00% 受 fact_id 重名影响而偏低。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
