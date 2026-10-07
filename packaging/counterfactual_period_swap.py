"""反事实交换测试：定位年报修正版 BERT 的上期偏置来源。

背景
----
年报修正版 BERT 在两处表现一致地偏向上期：
  模型自报 test.jsonl : quote_本期 72.95%  vs  quote_上期 95.65%
  人工 Gold（打乱集）  : 本期 68%          vs  上期 98%
且其训练集已精确配平（train 正例 本期 399 / 上期 399），所以偏差**不是样本量失衡**造成的。

三种可能来源，本脚本用交换实验分离：
  A 句式：模型可能只依赖「本期/上期」这样的词面，而不看数值。
     → 交换题干的期间词、保留数值原状，看选择是否跟着词变。
  B 数值位置：模型可能依赖「两个数值谁在当前句子里」的位置线索。
     → 交换两个数值、保留期间词，看选择是否跟着值变。
  C 评分阈值：本期与上期的候选池构成不同（本期常带「未标明」口径的干扰项）。
     → 统计本期/上期候选池的 scope 分布差异。

三次运行
--------
  原始        ：不改变
  交换期间词  ：claim 里 本期↔上期、本报告期↔上年同期 互换，数值不动
  交换数值    ：claim 里出现的本期值与上期值互换，期间词不动
若模型对「期间词」敏感而对「数值」不敏感（或反之），即可定位偏置来源。

用法：
    python packaging/counterfactual_period_swap.py --device cpu
"""
from __future__ import annotations

import argparse
import json
import re
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

GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'
GOLD = GD / 'human_gold_20261004_shuffled.jsonl'
MODEL_DS = ROOT / '答辩评测' / 'annual_reports_weak_training_repaired_20261004'
OUT = GD / 'counterfactual_period_swap_report.json'

SWAP_PAIRS = [
    ('本报告期', '上年同期'), ('报告期内', '上年同期'), ('本期', '上期'),
    ('本年度', '上年度'), ('当期', '去年同期'),
]
PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%')
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?[)]?')


def swap_period_words(claim: str) -> tuple[str, int]:
    """互换期间词，数值不动。用占位符避免二次替换。"""
    out = claim
    n = 0
    for a, b in SWAP_PAIRS:
        if a in out or b in out:
            out = out.replace(a, '\x00A\x00').replace(b, '\x00B\x00')
            out = out.replace('\x00A\x00', b).replace('\x00B\x00', a)
            n += 1
    return out, n


def swap_values(claim: str, cur_val, pri_val) -> tuple[str, int]:
    """互换 claim 中出现的本期值与上期值，期间词不动。"""
    if cur_val is None or pri_val is None:
        return claim, 0
    n = 0
    # 以字符串形式在两值间互换（含千分位/小数变体都尝试）
    cands = []
    for v in (cur_val, pri_val):
        for fmt in (f'{v:,}', f'{v}', f'{v:,}'.rstrip('0').rstrip('.') if '.' in f'{v:,}' else f'{v}'):
            if fmt:
                cands.append(fmt)
    out = claim
    for fmt in sorted(set(cands), key=len, reverse=True):
        if fmt in out:
            out = out.replace(fmt, '\x00X\x00')
            n += 1
            break
    return out, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--model', default='annual_repaired_v1')
    args = ap.parse_args()

    import evaluate_repaired_annual_benchmark as ev

    report = {'model': args.model, 'runs': {}}

    # ================= 数据集 1：人工 Gold =================
    rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r['claim_id']].append(r)
    print(f'=== 数据集 1：人工 Gold（打乱集）{len(grouped)} 题 ===')

    for tag, mutate in (('原始', None), ('交换期间词', 'period'), ('交换数值', 'value')):
        # 关键：交换期间词后，**正确答案也要跟着换**。
        # 否则测的是「模型是否固执地坚持原答案」，而不是「模型的答案是否随期间表达改变」。
        relabel: dict[str, set[str]] = {}
        if mutate is None:
            work = [dict(r) for r in rows]
        else:
            work = []
            for cid, g in grouped.items():
                cur = next((c for c in g if c['label'] == 1 and c['period'] == '本期'), None)
                pri = next((c for c in g if c['label'] == 1 and c['period'] == '上期'), None)
                if mutate == 'period' and cur and pri:
                    # 期间词互换 → 正确答案在两期之间对调
                    relabel[cid] = ({pri['fact_id']} if g[0]['task_type'] == 'quote_current'
                                    else {cur['fact_id']})
                for r in g:
                    nr = dict(r)
                    claim = r['claim_text']
                    if mutate == 'period':
                        nc, _ = swap_period_words(claim)
                        nr['claim_text'] = nc
                    else:
                        nc, _ = swap_values(claim,
                                            cur.get('fact_value') if cur else None,
                                            pri.get('fact_value') if pri else None)
                        nr['claim_text'] = nc
                    work.append(nr)

        scores = ev.score_rows(work, args.model, batch_size=args.batch_size, device=args.device)
        sm = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(work, scores)}
        wg: dict[str, list[dict]] = defaultdict(list)
        for r in work:
            wg[r['claim_id']].append(r)

        stat = defaultdict(lambda: [0, 0])
        follows = defaultdict(lambda: [0, 0])   # 答案是否"跟着交换变"
        for cid, g in wg.items():
            gold_t = grouped[cid][0]['task_type']
            if gold_t == 'growth_set':
                continue
            gold = relabel.get(cid) or {c['fact_id'] for c in g if c['label'] == 1}
            ordered = sorted(g, key=lambda r: (-sm.get((cid, r['fact_id']), 0.0), r['candidate_position']))
            picked = ordered[0]['fact_id']
            hit = {picked} == gold
            stat[gold_t][0] += int(hit)
            stat[gold_t][1] += 1
            if mutate == 'period':
                orig = {c['fact_id'] for c in g if c['label'] == 1}
                follows[gold_t][0] += int(picked not in orig)   # 换了选择
                follows[gold_t][1] += 1
        res = {t: round(v[0] / v[1], 4) for t, v in stat.items()}
        report['runs'][f'gold_{tag}'] = res
        if mutate == 'period':
            report['gold_follows_swap'] = {t: round(v[0] / v[1], 4) for t, v in follows.items()}
        print(f'  {tag:12} 本期 {res.get("quote_current", 0)*100:>6.2f}%  上期 {res.get("quote_prior", 0)*100:>6.2f}%'
              + (f"   | 答案随期间词改变的比例 {report.get('gold_follows_swap')}" if mutate == 'period' else ''))

    # ========== 数据集 2：模型自带 train/dev/test ==========
    print()
    print('=== 数据集 2：年报修正版自带 split ===')
    for split in ('train', 'dev', 'test'):
        p = MODEL_DS / f'{split}.jsonl'
        if not p.is_file():
            continue
        mrows = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
        mg: dict[str, list[dict]] = defaultdict(list)
        for r in mrows:
            mg[r['claim_id']].append(r)
        # 只取单值题
        for tag, mutate in (('原始', None), ('交换期间词', 'period')):
            work = []
            for cid, g in mg.items():
                tt = g[0].get('task_type') or ''
                if 'quote' not in tt:
                    continue
                for r in g:
                    nr = dict(r)
                    if mutate == 'period':
                        nc, _ = swap_period_words(r['claim_text'])
                        nr['claim_text'] = nc
                    work.append(nr)
            if not work:
                continue
            scores = ev.score_rows(work, args.model, batch_size=args.batch_size, device=args.device)
            sm = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(work, scores)}
            wg: dict[str, list[dict]] = defaultdict(list)
            for r in work:
                wg[r['claim_id']].append(r)
            stat = defaultdict(lambda: [0, 0])
            for cid, g in wg.items():
                gold = {c['fact_id'] for c in g if c['label'] == 1}
                # 模型数据集可能没有 candidate_position，退回按出现顺序稳定排序
                ordered = sorted(g, key=lambda r: (-sm.get((cid, r['fact_id']), 0.0),
                                                   r.get('candidate_position', 0)))
                hit = {ordered[0]['fact_id']} == gold
                tt = g[0].get('task_type')
                stat[tt][0] += int(hit)
                stat[tt][1] += 1
            res = {t: round(v[0] / v[1], 4) for t, v in stat.items()}
            report['runs'][f'{split}_{tag}'] = res
            print(f'  {split} {tag:12} ' + '  '.join(f'{t} {v*100:.2f}%' for t, v in sorted(res.items())))

    # ========== C) 候选池 scope 构成差异 ==========
    print()
    print('=== C) 本期/上期候选池的 scope 构成（查评分阈值类成因）===')
    sc = defaultdict(Counter)
    for cid, g in grouped.items():
        gt = g[0]['task_type']
        if gt == 'growth_set':
            continue
        for c in g:
            if c['label'] == 1:
                sc[c['period']][c.get('scope')] += 1
        for c in g:
            if c['label'] == 0:
                sc[c['period']][f'负:{c.get("scope")}'] += 1
    for per in ('本期', '上期'):
        print(f'  {per}: {dict(sc[per])}')
    report['scope_composition'] = {k: dict(v) for k, v in sc.items()}

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print()
    print(f'→ {OUT.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
