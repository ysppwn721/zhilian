"""按公司隔离的 train/dev/test 划分方案（dry-run，不写死划分）。

要求文档的硬约束：
  dev  ≥ 100 条论断、≥10 家独立公司
  test ≥ 200 条论断、≥15 家独立公司
  train 与 dev/test 必须公司不重叠（含同公司跨年、修订版）
  同一公司的所有年份/修订版留在同一划分

本脚本按**可用候选数**（而不是报告数）来分配公司，
使 dev/test 达到"条数"要求，同时给出是否达标的判断。
"""
from __future__ import annotations

import argparse
import hashlib
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    ap.add_argument('--dev-claims', type=int, default=100)
    ap.add_argument('--test-claims', type=int, default=200)
    args = ap.parse_args()

    d = args.corpus
    # 读**已准入**的候选，而不是原始候选。
    # 原始 candidates_v2.jsonl 含未过口径/三值/单位门槛的记录（301 条），
    # 用它会高估可划分规模、并让划分与训练集不一致（实测 301 vs 242）。
    admitted_path = d / 'training_candidates.jsonl'
    src_path = admitted_path if admitted_path.is_file() else (d / 'candidates_v2.jsonl')
    cs = [json.loads(l) for l in src_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    if src_path is admitted_path:
        usable = cs
    else:
        usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]
    print(f'划分数据源：{src_path.name}（{len(usable)} 条）\n')
    mf = [json.loads(l) for l in (d / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]

    per_company = Counter(c['company'] for c in usable)
    companies = sorted({r['stock_code'] for r in mf})
    # 确定性打散：按公司代码的哈希排序，避免按代码顺序造成行业/年份聚集
    companies.sort(key=lambda c: hashlib.sha256(c.encode()).hexdigest())

    # 关键：184 家里只有约 48 家有候选。若顺序填充分配，先填的划分会把有候选的公司
    # 全部吃光（实测 test 独占 48 家 140 条，dev/train 归零）。
    # 改为按候选数降序、在公司之间**轮流**投放到 test → dev → train，
    # 使三个划分都拿到高产出公司，同时保持公司隔离。
    with_claims = [c for c in companies if per_company.get(c, 0) > 0]
    without = [c for c in companies if per_company.get(c, 0) == 0]
    with_claims.sort(key=lambda c: (-per_company[c],
                                    hashlib.sha256(c.encode()).hexdigest()))

    test, dev, train = [], [], []
    t_sum = dv_sum = 0
    order = ('test', 'dev', 'train')
    for i, c in enumerate(with_claims):
        slot = order[i % 3]
        if slot == 'test':
            test.append(c)
            t_sum += per_company[c]
        elif slot == 'dev':
            dev.append(c)
            dv_sum += per_company[c]
        else:
            train.append(c)
    train += without

    print('=== 划分方案（按公司隔离，候选条数分配）===')
    print(f"  {'划分':6} {'公司数':>6} {'候选条数':>9}  要求")
    print(f"  {'train':6} {len(train):>6} {sum(per_company.get(c,0) for c in train):>9}")
    ok_dev = dv_sum >= args.dev_claims and len(dev) >= 10
    ok_test = t_sum >= args.test_claims and len(test) >= 15
    print(f"  {'dev':6} {len(dev):>6} {dv_sum:>9}  ≥{args.dev_claims} 条且≥10 家  "
          f"{'✓ 达标' if ok_dev else '✗ 不足'}")
    print(f"  {'test':6} {len(test):>6} {t_sum:>9}  ≥{args.test_claims} 条且≥15 家  "
          f"{'✓ 达标' if ok_test else '✗ 不足'}")
    print()
    print(f"  合计公司 {len(companies)} 家，可用候选 {len(usable)} 条")
    print()
    print('  覆盖率检查：')
    print(f"    有候选的公司 {len(per_company)}/{len(companies)} "
          f"（{len(companies)-len(per_company)} 家零候选）")
    print(f"    零候选的公司全部进 train（不影响 dev/test 条数）")

    # 缺口
    print()
    if not ok_test:
        need = args.test_claims - t_sum
        print(f"  ⚠ test 还差 {need} 条候选 —— 需要继续扩采"
              f"（按当前 0.76 条/份，约需 {need/0.76:.0f} 份）")
    if not ok_dev:
        need = args.dev_claims - dv_sum
        print(f"  ⚠ dev 还差 {need} 条候选（约需 {need/0.76:.0f} 份）")
    if ok_dev and ok_test:
        total_need = args.dev_claims + args.test_claims
        print(f"  ✓ dev/test 均达标。train 现有 {sum(per_company.get(c,0) for c in train)} 条，"
              f"目标 300–500 条正例还需扩采 {(500-sum(per_company.get(c,0) for c in train))/0.76:.0f}–"
              f"{(300-sum(per_company.get(c,0) for c in train))/0.76:.0f} 份")

    print()
    print('=== 行业在各划分的分布（应大致均衡）===')
    ind_of = {r['stock_code']: r['industry'] for r in mf}
    for name, group in (('train', train), ('dev', dev), ('test', test)):
        print(f"  {name:6} {dict(Counter(ind_of.get(c,'?') for c in group).most_common())}")

    print()
    print('=== 逐年检查：同一公司是否跨划分（应为 0）===')
    by_code_splits = defaultdict(set)
    for name, group in (('train', train), ('dev', dev), ('test', test)):
        for c in group:
            by_code_splits[c].add(name)
    leak = {c: s for c, s in by_code_splits.items() if len(s) > 1}
    print(f"  跨划分公司: {len(leak)}  {'✓' if not leak else '✗ ' + str(leak)}")

    print()
    print('  本脚本只输出方案，不写划分文件。正式划分需在标签审计后执行。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
