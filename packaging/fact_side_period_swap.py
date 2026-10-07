"""决定性偏置测试：只交换 fact 侧的期间标签，claim 保持原样。

为什么需要这个测试
------------------
上一个实验交换 claim 的期间词，会与 fact 文本产生矛盾
（claim 说「上期」，fact 仍写 period=上期，但那是原本期值），
于是准确率下降混入了「模型对」与「模型被矛盾干扰」两种成分，无法分离。

本测试的设计
------------
claim 完全不动；把每条 fact_text 里的 period 从 本期↔上期 互换。
两期事实的 (metric, scope, value, 位置) 一律不变，**只有期间标签变了**。

于是正确选择必然翻转：
  - 若模型的选择**跟着翻转** → 它的判断依赖期间信息，行为正确
  - 若模型的选择**不变** → 它根本没在用期间信息，偏置是系统性的

指标：flip_rate = 交换后选择改变的比例（理想=100%）
      同侧率   = 交换后仍选原那一侧的比例（= 1 - flip_rate）
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
OUT = GD / 'fact_side_period_swap_report.json'
PERIOD_IN_TEXT = re.compile(r'period=(本期|上期)(?=；|$)')


def swap_fact_period(text: str) -> str:
    def rep(m):
        return 'period=' + ('上期' if m.group(1) == '本期' else '本期')
    return PERIOD_IN_TEXT.sub(rep, text or '')


def run(rows: list[dict], tag: str, ev, args) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r['claim_id']].append(r)

    # 原始
    s0 = ev.score_rows(rows, args.model, batch_size=args.batch_size, device=args.device)
    sm0 = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(rows, s0)}

    # fact 侧期间标签互换
    swapped = []
    for r in rows:
        nr = dict(r)
        nr['fact_text'] = swap_fact_period(r['fact_text'])
        swapped.append(nr)
    s1 = ev.score_rows(swapped, args.model, batch_size=args.batch_size, device=args.device)
    sm1 = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(swapped, s1)}

    stat = defaultdict(lambda: [0, 0])       # 原始正确率
    flip = defaultdict(lambda: [0, 0])       # 选择翻转率
    for cid, g in grouped.items():
        gold = {c['fact_id'] for c in g if c['label'] == 1}
        o0 = sorted(g, key=lambda r: (-sm0.get((cid, r['fact_id']), 0.0),
                                      r.get('candidate_position', 0)))
        o1 = sorted(g, key=lambda r: (-sm1.get((cid, r['fact_id']), 0.0),
                                      r.get('candidate_position', 0)))
        p0, p1 = o0[0]['fact_id'], o1[0]['fact_id']
        task = g[0].get('task_type') or ''
        stat[task][0] += int({p0} == gold)
        stat[task][1] += 1
        flip[task][0] += int(p1 != p0)
        flip[task][1] += 1

    res = {t: {'n': v[1], 'orig_acc': round(v[0] / v[1], 4),
               'flip_rate': round(flip[t][0] / max(1, flip[t][1]), 4)}
           for t, v in stat.items()}
    print(f'  [{tag}]')
    for t, v in sorted(res.items()):
        print(f'     {t:16} n={v["n"]:>4}  原始正确 {v["orig_acc"]*100:>6.2f}%  '
              f'选择翻转率 {v["flip_rate"]*100:>6.2f}%')
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--model', default='annual_repaired_v1')
    args = ap.parse_args()

    import evaluate_repaired_annual_benchmark as ev

    report = {'model': args.model, 'design': 'fact 侧 period 标签互换，claim 不动'}
    print('=== 人工 Gold（打乱集，单值题）===')
    rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
    rows = [r for r in rows if r['task_type'] in ('quote_current', 'quote_prior')]
    report['gold'] = run(rows, 'gold', ev, args)

    print()
    print('=== 年报修正版自带 split（单值题）===')
    for split in ('train', 'dev', 'test'):
        p = MODEL_DS / f'{split}.jsonl'
        if not p.is_file():
            continue
        mrows = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
        mrows = [r for r in mrows if 'quote' in (r.get('task_type') or '')]
        report[split] = run(mrows, split, ev, args)

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print()
    print('判读：')
    print('  flip_rate ≈ 100% → 模型依赖期间信息，原偏差可能来自别处')
    print('  flip_rate 明显 < 100% → 模型中有一部分判断不随期间改变，属系统性偏置')
    print(f'→ {OUT.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
