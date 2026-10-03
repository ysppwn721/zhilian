"""把多批采集的年报合并成一个统一语料目录（用硬链接，不复制文件）。

为什么需要合并
--------------
批次目录是分开采的（避免混入旧语料），但构造候选与划分 train/dev/test 必须看到
全部公司——按公司隔离划分要求 dev/test 与 train 不同公司，只有合并后才能做。

实现
----
- 硬链接（os.link）指向原 PDF：同一磁盘碎片，不占额外空间；
  若文件系统不支持硬链接则回退为复制。
- manifest.jsonl 合并并补齐 source_batch 字段，便于回溯到哪一批。
- 跨批次公司去重检查：同一公司出现在多批时告警（要求文档要求公司隔离）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent


def link_or_copy(src: Path, dst: Path) -> str:
    if dst.exists():
        return 'exists'
    try:
        os.link(src, dst)
        return 'linked'
    except OSError:
        shutil.copyfile(src, dst)
        return 'copied'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    ap.add_argument('--batches', nargs='+', required=True,
                    help='各批次目录（含 manifest.jsonl）')
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    merged, stats = [], Counter()
    for rel in args.batches:
        b = Path(rel)
        if not b.is_absolute():
            b = ROOT / b
        mf = b / 'manifest.jsonl'
        if not mf.is_file():
            print(f'  [警告] 缺少清单，跳过：{mf}')
            continue
        batch_name = b.name
        for line in mf.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            rec['source_batch'] = batch_name
            src = b / rec['local_file']
            if not src.is_file():
                stats['missing_source'] += 1
                continue
            action = link_or_copy(src, args.out / rec['local_file'])
            stats[action] += 1
            merged.append(rec)

    # 跨批次公司重复检查（公司隔离的前提）
    by_code = Counter(r['stock_code'] for r in merged)
    dup = {c: n for c, n in by_code.items() if n > 1}
    if dup:
        print(f'\n⚠ 跨批次重复公司 {len(dup)} 家（违反公司隔离，需处理）：')
        for c, n in sorted(dup.items())[:20]:
            files = [r['local_file'] for r in merged if r['stock_code'] == c]
            print(f'    {c} × {n}: {files}')
    else:
        print('\n✓ 跨批次无重复公司（公司隔离成立）')

    (args.out / 'manifest.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in merged), encoding='utf-8')

    print(f'\n合并 {len(merged)} 份 → {args.out.name}/')
    print(f'  硬链接 {stats["linked"]} · 复制 {stats["copied"]} · 已存在 {stats["exists"]} · 源缺失 {stats["missing_source"]}')
    print(f'  批次分布: {dict(Counter(r["source_batch"] for r in merged))}')
    print(f'  独立公司: {len(by_code)}')
    print(f'  行业分布: {dict(Counter(r["industry"] for r in merged).most_common())}')
    print(f'  年份分布: {dict(sorted(Counter(r["report_year"] for r in merged).items()))}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
