"""诊断：63.89% 是「学到了但泛化不足」还是「什么都没学到」。

判据是**简单基线**。若某个不看语义的规则（总选本期 / 总选上期 / 总选第一个）
也能拿到接近甚至超过 63.89%，则模型没有学到可泛化的关联能力。

同时检查 holdout 本身是否存在捷径（历史审计曾在另一套数据上发现
「总选本期 = 100%」，使基准失效）。
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
HOLDOUT = ROOT / '答辩评测' / 'external_programmatic_holdout_all.jsonl'


def main() -> int:
    rows = [json.loads(l) for l in HOLDOUT.read_text(encoding='utf-8').splitlines() if l.strip()]
    print(f'holdout {len(rows)} 行')
    print('首行字段:', sorted(rows[0].keys()))
    print()

    # 按 claim 分组
    by_claim: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_claim[r.get('claim_id') or r.get('group_id')].append(r)
    print(f'claim 数 {len(by_claim)}')
    print()

    # 候选构成
    print('=== 候选构成 ===')
    per = Counter(len(v) for v in by_claim.values())
    print(f'  每 claim 候选数分布: {dict(sorted(per.items()))}')
    label_dist = Counter()
    for v in by_claim.values():
        label_dist[sum(1 for r in v if r.get('label') == 1)] += 1
    print(f'  每 claim 正例数分布: {dict(sorted(label_dist.items()))}')
    print()

    # fact_id 形态
    print('=== fact_id 形态（看是否含可被规则利用的后缀）===')
    suffix = Counter()
    for r in rows:
        fid = str(r.get('fact_id') or r.get('id') or '')
        m = re.search(r'[-_]([a-z]+)$', fid)
        suffix[m.group(1) if m else '（无后缀）'] += 1
    print(f'  {dict(suffix.most_common(10))}')
    print()

    # ---- 简单基线 ----
    def pick(order_key):
        """按 order_key 给候选排序，取第一个，看其 label。"""
        hit = 0
        for cid, cands in by_claim.items():
            ordered = sorted(cands, key=order_key)
            if ordered and ordered[0].get('label') == 1:
                hit += 1
        return hit / max(1, len(by_claim))

    def fid_of(r):
        return str(r.get('fact_id') or r.get('id') or '')

    baselines = {
        '总选 fact_id 以 -current 结尾': pick(lambda r: 0 if fid_of(r).endswith('-current') else 1),
        '总选 fact_id 以 -prior 结尾': pick(lambda r: 0 if fid_of(r).endswith('-prior') else 1),
        '总选第一个候选': pick(lambda r: 0),
        '总选最后一个候选': pick(lambda r: -0),
    }
    # 按 period 字段
    def period_key(r):
        p = str(r.get('period') or '')
        return 0 if '本期' in p else (1 if '上期' in p else 2)
    baselines['总选 period=本期'] = pick(period_key)
    def period_key_prior(r):
        p = str(r.get('period') or '')
        return 0 if '上期' in p else (1 if '本期' in p else 2)
    baselines['总选 period=上期'] = pick(period_key_prior)

    print('=== 简单基线（不看语义）===')
    print(f'  {"规则":32} {"Top-1":>8}')
    for k, v in sorted(baselines.items(), key=lambda kv: -kv[1]):
        print(f'  {k:32} {v*100:>7.2f}%')
    print()
    print(f'  模型实际 Top-1: 63.89%')
    best = max(baselines.values())
    margin = 0.6389 - best
    print()
    if best >= 0.6389:
        print(f'  ✗ 最强基线 {best*100:.2f}% 已达到或超过模型 —— 模型没有学到可泛化的关联能力。')
    elif margin < 0.10:
        print(f'  △ 模型仅比最强基线高 {margin*100:.1f} 个百分点 —— 提升有限，需看是否显著。')
    else:
        print(f'  ✓ 模型比最强基线高 {margin*100:.1f} 个百分点 —— 确实学到了东西，问题在泛化不足。')
    print()

    # 训练集自身的捷径检查
    print('=== 训练集自身的捷径检查（是否也存在"总选本期"就满分）===')
    tr = ROOT / '答辩评测' / 'annual_reports_final' / 'negatives_v1.jsonl'
    if tr.is_file():
        trows = [json.loads(l) for l in tr.read_text(encoding='utf-8').splitlines() if l.strip()]
        tby: dict[str, list[dict]] = defaultdict(list)
        for r in trows:
            tby[r['claim_id']].append(r)
        t_hit = 0
        n = 0
        for cid, cands in tby.items():
            if not any(c.get('label') == 1 for c in cands):
                continue
            n += 1
            # 规则：总选 fact_text 里 period=本期 且 scope=合并 的那条
            ordered = sorted(cands, key=lambda c: 0 if ('period=本期' in c.get('fact_text', '')
                                                        and 'scope=合并' in c.get('fact_text', '')) else 1)
            if ordered and ordered[0].get('label') == 1:
                t_hit += 1
        print(f'  训练集「总选 本期+合并」: {t_hit/max(1,n)*100:.2f}%  (n={n})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
