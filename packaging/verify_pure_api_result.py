"""纯 API 结果的独立核验。

两个问题必须回答，否则 92% 不可解读：
  1. 简单规则（只按 metric + period + scope 字段匹配）在同一打乱集上能拿多少？
     若规则也接近 92%，则 API 没有超出字段匹配的增量。
  2. 失败的 12 题是模型问题，还是 Gold 本身有歧义（多条候选都符合指令）？
"""
from __future__ import annotations

import json
import re
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

# 指令中的期间词 → 应匹配的 period 值
PERIOD_HINT = [
    (re.compile(r'上一报告期|上期|上年同期|上年度|去年同期'), '上期'),
    (re.compile(r'本报告期|本期|报告期内|报告期'), '本期'),
]
SCOPE_PREF = ['合并', '母公司', '分部']


def period_of(claim: str) -> str | None:
    for pat, p in PERIOD_HINT:
        if pat.search(claim):
            return p
    return None


def main() -> int:
    rows = [json.loads(l) for l in (GD / 'human_gold_20261004_shuffled.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    byq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byq[r['claim_id']].append(r)
    preds = {json.loads(l)['claim_id']: json.loads(l)
             for l in (GD / 'human_gold_pure_api_20261004.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()}

    qs = [sorted(v, key=lambda r: r['candidate_position']) for v in byq.values()]

    # ---------- 1) 规则基线 ----------
    def rule_pick(q, use_scope=True):
        claim = q[0]['claim_text']
        want_p = period_of(claim)
        # 指令里的指标名：从 gold_metric 不可得时用 gold 的 metric 反推（避免作弊，
        # 改为从指令文本里匹配候选指标名）
        cand_metrics = {c['metric'] for c in q}
        want_m = None
        for m in sorted(cand_metrics, key=len, reverse=True):
            if m and m in claim:
                want_m = m
                break
        if want_m is None:
            # 同义说法（营收/净利）
            alias = {'营收': '营业收入', '净利': '净利润'}
            for a, full in alias.items():
                if a in claim:
                    want_m = full
                    break
        pool = [c for c in q if (want_m is None or c['metric'] == want_m)
                and (want_p is None or c['period'] == want_p)]
        if not pool:
            pool = [c for c in q if want_p is None or c['period'] == want_p] or q
        if use_scope:
            for pref in SCOPE_PREF:
                hit = [c for c in pool if c.get('scope') == pref]
                if hit:
                    pool = hit
                    break
        gold = {c['fact_id'] for c in q if c['label'] == 1}
        return pool, gold

    for use_scope in (False, True):
        ok = 0
        for q in qs:
            pool, gold = rule_pick(q, use_scope)
            pred = {pool[0]['fact_id']} if len(gold) == 1 and pool else {c['fact_id'] for c in pool[:2]}
            if pred == gold:
                ok += 1
        tag = '规则(指标+期间+口径偏好)' if use_scope else '规则(仅指标+期间)'
        print(f'  {tag:26} {ok:>3}/150  {ok/150*100:>6.2f}%')

    print()
    # ---------- 2) API 分层结果 ----------
    print('=== 纯 API 分层结果 ===')
    for task in ('quote_current', 'quote_prior', 'growth_set'):
        sel = [q for q in qs if q[0]['task_type'] == task]
        ok = sum(1 for q in sel if preds[q[0]['claim_id']]['exact_match'])
        print(f'  {task:14} {ok:>3}/{len(sel)}  {ok/len(sel)*100:>6.2f}%')
    print()

    # 位置分层（确认打乱后无位置依赖）
    print('=== 打乱后：正例位置 vs 是否答对 ===')
    hit_pos, miss_pos = Counter(), Counter()
    for q in qs:
        cid = q[0]['claim_id']
        gp = {c['candidate_position'] for c in q if c['label'] == 1}
        tgt = hit_pos if preds[cid]['exact_match'] else miss_pos
        for p in gp:
            tgt[p] += 1
    print(f'  答对题的 gold 位置分布(前8): {dict(sorted(hit_pos.items())[:8])}')
    print(f'  答错题的 gold 位置分布    : {dict(sorted(miss_pos.items()))}')
    print()

    # ---------- 3) 错题分析 ----------
    print('=== 12 道错题：模型问题还是 Gold 歧义？ ===')
    wrong = [q for q in qs if not preds[q[0]['claim_id']]['exact_match']]
    ambiguous = 0
    for q in wrong:
        cid = q[0]['claim_id']
        p = preds[cid]
        gold = set(p['gold_fact_ids'])
        pred = set(p['pred_fact_ids'])
        # 判定歧义：模型选的候选与 gold 在 (metric, period) 上相同，仅 scope 不同
        gset = {(c['metric'], c['period']) for c in q if c['fact_id'] in gold}
        pset = {(c['metric'], c['period']) for c in q if c['fact_id'] in pred}
        ambiguous += int(bool(pset) and pset == gset and pred != gold)
        print(f"  [{q[0]['task_type']:<14}] {cid}")
        print(f"     指令: {q[0]['claim_text'][:58]}")
        print(f"     gold: {sorted(gold)}")
        print(f"     预测: {sorted(pred)}  ({p['parse_mode']})")
        gp = [c for c in q if c['fact_id'] in gold]
        pp = [c for c in q if c['fact_id'] in pred]
        for c in gp:
            print(f"       ✓ {c['metric'][:26]:<26} period={c['period']:<5} scope={c.get('scope')}")
        for c in pp:
            print(f"       ✗ {c['metric'][:26]:<26} period={c['period']:<5} scope={c.get('scope')}")
        print()
    print(f'  其中"指标与期间都对、仅口径不同"的歧义错题: {ambiguous}')
    print(f'  其余（指标或期间选错）            : {len(wrong)-ambiguous}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
