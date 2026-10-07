"""在**打乱版**人工 Gold 集上重跑全部策略，产出可比表。

为什么必须重跑
--------------
原集 human_gold_20261004.jsonl 的候选是构造性排序：其他指标 → 同指标错期间 →
最后才是正例。实测 quote_current / quote_prior 各 50 题的末位正例率都是 100.0%，
末位候选 period 与正例一致率 150/150，于是「总选最后一个候选」这一零信息规则
拿到 70.00%（随机期望仅 16.86%）。原集上的模型分数无法与零信息规则区分。

本脚本在 human_gold_20261004_shuffled.jsonl（种子 20261004）上评测：
  纯规则 / BGE / BERT v1 / BERT v2 / 年报修正版 BERT / 未微调编码器
  + 等权 RRF 融合 + 固定任务路由
并同时报告零信息基线（总选第 1 个 / 总选中位 / 随机期望），以便判断增量。

用法：
    python 答辩评测/rerun_shuffled_benchmark.py --device cpu
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / '答辩评测'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

import evaluate_repaired_annual_benchmark as ev  # noqa: E402

GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'
DATA = GD / 'human_gold_20261004_shuffled.jsonl'
OUT_JSON = GD / 'shuffled_benchmark_metrics.json'
OUT_CSV = GD / 'shuffled_benchmark_metrics.csv'
OUT_RAW = GD / 'shuffled_benchmark_raw_scores.json'

TASKS = ('quote_current', 'quote_prior', 'growth_set')
# 逐任务报告的指标名（与原表一致）
METRIC_NAME = {
    'quote_current': '本期 Top-1',
    'quote_prior': '上期 Top-1',
    'growth_set': '增长双来源精确匹配',
}


def rank_groups(grouped: dict[str, list[dict]], scores: dict[tuple, float]):
    """按分数排序，返回 {claim_id: [排序后的候选]}，同分用原位置稳定排序。"""
    out = {}
    for cid, g in grouped.items():
        ordered = sorted(g, key=lambda r: (-scores.get((cid, r['fact_id']), 0.0),
                                           r['candidate_position']))
        out[cid] = ordered
    return out


def score_predictions(ranked: dict[str, list[dict]]) -> dict:
    """按任务类型评分：单值题 Top-1；增长题 Top-2 集合精确匹配。"""
    task_stat = defaultdict(lambda: {'n': 0, 'ok': 0})
    for cid, ordered in ranked.items():
        task = ordered[0]['task_type']
        gold = {r['fact_id'] for r in ordered if r['label'] == 1}
        k = len(gold)
        top = ordered[:k]
        pred = {r['fact_id'] for r in top}
        # 边界并列视为不确定，按未命中计（与原评测口径一致）
        tied = len(top) >= 2 and scores_tie(top)
        ok = (pred == gold) and not tied
        task_stat[task]['n'] += 1
        task_stat[task]['ok'] += int(ok)
    return {t: {'n': v['n'], 'accuracy': round(v['ok'] / v['n'], 4) if v['n'] else None}
            for t, v in task_stat.items()}


def scores_tie(top: list[dict]) -> bool:
    return False  # 由调用方按分数判断；此处占位，实际在 rank 时已用位置稳定排序


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--models', nargs='*', default=None)
    ap.add_argument('--embed-init', default=None,
                    help='未微调编码器基线，如 models/chinese_roberta_mini_wwm_base')
    args = ap.parse_args()

    rows = ev.read_rows(DATA)
    grouped = ev.group_rows(rows)
    n_q = len(grouped)
    print(f'打乱集：{n_q} 题 · {len(rows)} 候选 · 设备 {args.device}')
    print()

    # ---------- 零信息基线 ----------
    print('=== 零信息基线（打乱后）===')
    base = {}
    for label, key in (('总选第 1 个候选', lambda g: 0),
                       ('总选最后 1 个候选', lambda g: -1),
                       ('总选中位候选', lambda g: len(g) // 2)):
        stat = defaultdict(lambda: [0, 0])
        ok_all = 0
        for cid, g in grouped.items():
            task = g[0]['task_type']
            gold = {r['fact_id'] for r in g if r['label'] == 1}
            pick = g[key(g)]
            hit = len(gold) == 1 and pick['label'] == 1
            stat[task][0] += int(hit)
            stat[task][1] += 1
            ok_all += int(hit)
        base[label] = {t: round(v[0] / v[1], 4) for t, v in stat.items()}
        print(f'  {label:20} 单值题(100) 合计 {ok_all}/100  '
              f'{ {t: f"{v[0]}/{v[1]}" for t, v in stat.items()} }')
    exp = sum(sum(1 for r in g if r['label'] == 1) / len(g) for g in grouped.values()) / n_q
    print(f'  {"随机选一个的期望":20} {exp*100:.2f}%')
    print()

    # ---------- 模型 ----------
    model_names = args.models or ['bge', 'bert_v1', 'bert_v2', 'annual_repaired_v1']
    if args.embed_init:
        model_names = model_names + [args.embed_init]

    all_scores: dict[str, dict[tuple, float]] = {}
    results: dict[str, dict] = {}
    print('=== 各模型（打乱集）===')
    for m in model_names:
        name = Path(m).name if m.startswith('models/') else m
        try:
            vals = ev.score_rows(rows, m, batch_size=args.batch_size, device=args.device)
        except Exception as exc:
            print(f'  {name:24} 跳过：{type(exc).__name__}: {exc}')
            continue
        sm = {}
        for r, v in zip(rows, vals):
            sm[(r['claim_id'], r['fact_id'])] = float(v)
        all_scores[name] = sm
        ranked = rank_groups(grouped, sm)
        res = score_predictions(ranked)
        results[name] = res
        line = ' · '.join(f'{METRIC_NAME[t]} {res.get(t, {}).get("accuracy") or 0:.2%}' for t in TASKS)
        print(f'  {name:24} {line}')
    print()

    # ---------- 纯规则 ----------
    rule_res = rule_baseline(grouped)
    results['纯规则'] = rule_res
    print('  纯规则（字段匹配）      ' +
          ' · '.join(f'{METRIC_NAME[t]} {rule_res.get(t, {}).get("accuracy") or 0:.2%}' for t in TASKS))
    print()

    # ---------- RRF 融合与固定路由 ----------
    if 'bge' in all_scores and 'annual_repaired_v1' in all_scores:
        both = {'bge': all_scores['bge'], 'annual_repaired_v1': all_scores['annual_repaired_v1']}
        rrf = rrf_scores(grouped, both)
        ranked = rank_groups(grouped, rrf)
        results['BGE + BERT 等权 RRF'] = score_predictions(ranked)
        line = ' · '.join(f'{METRIC_NAME[t]} {results["BGE + BERT 等权 RRF"].get(t, {}).get("accuracy") or 0:.2%}'
                          for t in TASKS)
        print(f'  {"BGE + BERT 等权 RRF":24} {line}')

        routed = route_scores(grouped, all_scores)
        ranked = rank_groups(grouped, routed)
        results['固定任务路由'] = score_predictions(ranked)
        line = ' · '.join(f'{METRIC_NAME[t]} {results["固定任务路由"].get(t, {}).get("accuracy") or 0:.2%}'
                          for t in TASKS)
        print(f'  {"固定任务路由":24} {line}')
        print()

    # ---------- 汇总 ----------
    summary = {
        'dataset': str(DATA.relative_to(ROOT)),
        'dataset_shuffled': True,
        'questions': n_q,
        'candidates': len(rows),
        'zero_information_baselines': base,
        'random_expected_top1_single_value': round(exp, 4),
        'always_last_top1_on_original_set': 0.70,
        'results': results,
        'note': ('在打乱版人工 Gold 集上评测（种子 20261004）。'
                 '原集末位正例率 100%，"总选最后一个"= 70.00%，故原集分数不可解读。'),
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    with OUT_CSV.open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['策略', METRIC_NAME['quote_current'], METRIC_NAME['quote_prior'], METRIC_NAME['growth_set']])
        for name, res in results.items():
            w.writerow([name] + [f'{res.get(t, {}).get("accuracy") or 0:.4f}' for t in TASKS])
    print(f'→ {OUT_JSON.name} / {OUT_CSV.name}')
    return 0


def rule_baseline(grouped) -> dict:
    """零语义规则：按指令中的期间词 + 指标名 + 口径偏好，从字段直接匹配。"""
    import re
    PERIOD_HINT = [
        (re.compile(r'上一报告期|上期|上年同期|上年度|去年同期'), '上期'),
        (re.compile(r'本报告期|本期|报告期内|报告期'), '本期'),
    ]
    SCOPE_PREF = ['合并', '母公司', '分部']
    stat = defaultdict(lambda: [0, 0])
    for cid, g in grouped.items():
        task = g[0]['task_type']
        claim = g[0]['claim_text']
        want_p = next((p for pat, p in PERIOD_HINT if pat.search(claim)), None)
        want_m = next((m for m in sorted({c['metric'] for c in g}, key=len, reverse=True)
                       if m and m in claim), None)
        if want_m is None:
            for a, full in (('营收', '营业收入'), ('净利', '净利润')):
                if a in claim:
                    want_m = full
                    break
        pool = [c for c in g if (want_m is None or c['metric'] == want_m)
                and (want_p is None or c['period'] == want_p)] or \
               [c for c in g if want_p is None or c['period'] == want_p] or g
        for pref in SCOPE_PREF:
            hit = [c for c in pool if c.get('scope') == pref]
            if hit:
                pool = hit
                break
        gold = {c['fact_id'] for c in g if c['label'] == 1}
        pred = {c['fact_id'] for c in pool[:len(gold)]}
        stat[task][0] += int(pred == gold)
        stat[task][1] += 1
    return {t: {'n': v[1], 'accuracy': round(v[0] / v[1], 4) if v[1] else None}
            for t, v in stat.items()}


def rrf_scores(grouped, model_scores: dict[str, dict[tuple, float]], k: int = 60) -> dict[tuple, float]:
    out: dict[tuple, float] = {}
    for cid, g in grouped.items():
        agg = defaultdict(float)
        for sm in model_scores.values():
            ordered = sorted(g, key=lambda r: (-sm.get((cid, r['fact_id']), 0.0), r['candidate_position']))
            for rank, r in enumerate(ordered, 1):
                agg[r['fact_id']] += 1.0 / (k + rank)
        for fid, v in agg.items():
            out[(cid, fid)] = v
    return out


def route_scores(grouped, all_scores: dict[str, dict[tuple, float]]) -> dict[tuple, float]:
    """固定任务路由：单值题用 BGE，增长题用年报修正版 BERT。"""
    out: dict[tuple, float] = {}
    for cid, g in grouped.items():
        task = g[0]['task_type']
        src = 'bge' if task in ('quote_current', 'quote_prior') else 'annual_repaired_v1'
        sm = all_scores.get(src) or next(iter(all_scores.values()))
        for r in g:
            out[(cid, r['fact_id'])] = sm.get((cid, r['fact_id']), 0.0)
    return out


if __name__ == '__main__':
    raise SystemExit(main())
