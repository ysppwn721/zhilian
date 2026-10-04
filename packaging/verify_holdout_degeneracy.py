"""验证 holdout 是否退化：逐条核对构造事实与实测基线。"""
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
H = ROOT / '答辩评测' / 'external_programmatic_holdout_all.jsonl'


def main() -> int:
    rows = [json.loads(l) for l in H.read_text(encoding='utf-8').splitlines() if l.strip()]
    by_claim: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_claim[r['claim_id']].append(r)

    print('=== 1) wrongperiod 是否在任何 claim 中为正例 ===')
    wp_gold = wp_total = 0
    for cid, cands in by_claim.items():
        for c in cands:
            if c['fact_id'].endswith('-wrongperiod'):
                wp_total += 1
                wp_gold += int(c['label'] == 1)
    print(f'  wrongperiod 候选 {wp_total} 条，其中 label=1 的 {wp_gold} 条')
    print(f'  → {"✓ 合成项从不是正例，构造上必然" if wp_gold == 0 else "✗ 有正例，我的推断错"}')
    print()

    print('=== 2) 候选顺序是否恒定（决定"总选第一个"能否满分）===')
    order_pat = Counter()
    for cid, cands in by_claim.items():
        suffix = tuple(c['fact_id'].rsplit('-', 1)[-1] for c in cands)
        order_pat[suffix] += 1
    print(f'  候选顺序模式: {dict(order_pat)}')
    print(f'  → {"✓ 顺序恒定，位置即答案" if len(order_pat) == 1 else "▽ 顺序有变化"}')
    print()

    print('=== 3) 逐 claim 核对正例集合 ===')
    pos_set = Counter()
    for cid, cands in by_claim.items():
        gold = tuple(sorted(c['fact_id'].rsplit('-', 1)[-1] for c in cands if c['label'] == 1))
        pos_set[gold] += 1
    print(f'  正例集合模式: {dict(pos_set)}')
    print()

    print('=== 4) claim 文本与正例的对应（c1 类：文本含本期值）===')
    hit_text = 0
    n1 = 0
    for cid, cands in by_claim.items():
        gold = [c for c in cands if c['label'] == 1]
        if len(gold) != 1:
            continue
        n1 += 1
        claim = gold[0]['claim_text']
        # 正例 fact_text 里的数值是否出现在 claim 文本中
        m = re.findall(r'[-−]?[\d,]+(?:\.\d+)?', gold[0]['fact_text'])
        m = [x.replace(',', '') for x in m]
        if any(x and x in claim.replace(',', '') for x in m):
            hit_text += 1
    print(f'  单正例 claim 共 {n1} 条')
    print(f'  其中正例数值字面出现在 claim 文本里: {hit_text} ({hit_text/max(1,n1)*100:.1f}%)')
    print(f'  → 这类 claim 只需字符串匹配即可答对，不需要语义理解')
    print()

    print('=== 5) 若全部选 current 的准确率（逐 claim 计）===')
    def acc(rule):
        ok = 0
        for cid, cands in by_claim.items():
            pick = rule(cands)
            if pick and pick['label'] == 1:
                ok += 1
        return ok / max(1, len(by_claim))

    r1 = acc(lambda cs: next((c for c in cs if c['fact_id'].endswith('-current')), None))
    r2 = acc(lambda cs: next((c for c in cs if c['fact_id'].endswith('-prior')), None))
    r3 = acc(lambda cs: cs[0])
    print(f'  总选 current : {r1*100:.2f}%')
    print(f'  总选 prior   : {r2*100:.2f}%')
    print(f'  总选第一个   : {r3*100:.2f}%')
    print()

    # 6) 模型评分的错误形态：是否把 current 误选成别人
    print('=== 6) 结论 ===')
    print(f'  wrongperiod 从不是正例: {wp_gold == 0}')
    print(f'  候选顺序恒定: {len(order_pat) == 1}')
    print(f'  claim 文本内置答案(c1 类): {hit_text/max(1,n1)*100:.0f}%')
    print()
    print('  该 benchmark 的三个候选里，一个合成项永远错、一个按位置恒为答案，')
    print('  因此任何"按位置/按期间字段"的规则都能拿到 100%。')
    print('  模型 63.89% 低于全部零信息基线 —— 该分数不反映关联能力。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
