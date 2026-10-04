"""构造位置打乱的人工 Gold 集。

为什么必须打乱
--------------
原集 human_gold_20261004.jsonl 的候选存在构造性排序：
  先列其他指标 → 再列同指标错期间 → **最后才是同指标对期间的正例**
实测 quote_current / quote_prior 各 50 题，末位是正例的比例都是 100.0%，
于是"总选最后一个候选"这一零信息规则在 150 题上拿到 70.00%，
而随机期望只有 16.86%。不消除它，任何模型在这两列上的分数都不可解读。

做法
----
每题内部用固定种子（20261004）做 Fisher-Yates 打乱，重排 candidate_position，
其余字段一律不动。同时输出核验：打乱后各位置正例率应接近均匀。

输出
----
  human_gold_20261004_shuffled.jsonl   打乱后的数据集
  human_gold_shuffle_report.json       打乱前后基线对照
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / '答辩评测' / 'human_gold_eval_20261004' / 'human_gold_20261004.jsonl'
DST = ROOT / '答辩评测' / 'human_gold_eval_20261004' / 'human_gold_20261004_shuffled.jsonl'
REP = ROOT / '答辩评测' / 'human_gold_eval_20261004' / 'human_gold_shuffle_report.json'
SEED = 20261004


def baselines(qs) -> dict:
    n = len(qs)
    exp = sum(sum(1 for r in q if r['label'] == 1) / len(q) for q in qs) / n
    return {
        'questions': n,
        'random_expected_top1': round(exp, 4),
        'always_first_top1': round(sum(1 for q in qs if q[0]['label'] == 1) / n, 4),
        'always_last_top1': round(sum(1 for q in qs if q[-1]['label'] == 1) / n, 4),
    }


def main() -> int:
    rows = [json.loads(l) for l in SRC.read_text(encoding='utf-8').splitlines() if l.strip()]
    byq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byq[r['claim_id']].append(r)

    # 打乱前基线
    before_qs = [list(v) for v in byq.values()]
    before = baselines(before_qs)

    # 每题独立打乱（固定种子，可复现）
    rng = random.Random(SEED)
    out_rows: list[dict] = []
    for cid in sorted(byq):
        items = list(byq[cid])
        rng.shuffle(items)
        for pos, r in enumerate(items):
            nr = dict(r)
            nr['candidate_position'] = pos
            nr['position_shuffled'] = True
            nr['original_position'] = r.get('candidate_position')
            out_rows.append(nr)

    DST.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in out_rows), encoding='utf-8')

    # 打乱后基线
    byq2: dict[str, list[dict]] = defaultdict(list)
    for r in out_rows:
        byq2[r['claim_id']].append(r)
    after_qs = [list(v) for v in byq2.values()]
    after = baselines(after_qs)

    # 位置正例率
    def pos_rate(qs):
        pos, tot = Counter(), Counter()
        for q in qs:
            for i, r in enumerate(q):
                pos[i] += (r['label'] == 1)
                tot[i] += 1
        return {i: (pos[i] / tot[i] if tot[i] else None, tot[i]) for i in sorted(tot)}

    rep = {
        'seed': SEED,
        'source': str(SRC.relative_to(ROOT)),
        'output': str(DST.relative_to(ROOT)),
        'rows': len(out_rows),
        'before': before,
        'after': after,
        'before_position_positive_rate': {k: round(v[0], 4) for k, v in pos_rate(before_qs).items() if v[1] >= 20},
        'after_position_positive_rate': {k: round(v[0], 4) for k, v in pos_rate(after_qs).items() if v[1] >= 20},
        'note': ('候选顺序为构造性排序（正例压末位），打乱后位置不再携带信息。'
                 '本文件用于消除位置伪影；模型分数须在打乱集上重测。'),
    }
    REP.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'打乱完成 → {DST.name}')
    print(f'  题目 {len(byq)} · 候选 {len(out_rows)} · 种子 {SEED}')
    print()
    print('=== 打乱前 ===')
    print(f'  总选第 1 个: {before["always_first_top1"]*100:>6.2f}%')
    print(f'  总选最后 1: {before["always_last_top1"]*100:>6.2f}%   ← 伪影')
    print(f'  随机期望  : {before["random_expected_top1"]*100:>6.2f}%')
    print()
    print('=== 打乱后 ===')
    print(f'  总选第 1 个: {after["always_first_top1"]*100:>6.2f}%')
    print(f'  总选最后 1: {after["always_last_top1"]*100:>6.2f}%')
    print(f'  随机期望  : {after["random_expected_top1"]*100:>6.2f}%')
    print()
    gap_b = abs(before['always_last_top1'] - before['random_expected_top1'])
    gap_a = abs(after['always_last_top1'] - after['random_expected_top1'])
    print(f'  "末位"基线与随机的差距: 打乱前 {gap_b*100:.2f} 点 → 打乱后 {gap_a*100:.2f} 点')
    print(f'  {"✓ 位置伪影已消除" if gap_a < 0.10 else "✗ 仍有位置信息"}')
    print()
    print('=== 打乱后各位置正例率（应接近均匀）===')
    for i, v in rep['after_position_positive_rate'].items():
        print(f'    位置 {i:>2}: {v*100:>6.2f}%')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
