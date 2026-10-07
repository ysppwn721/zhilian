"""列出可开源候选：按目录/类型统计未跟踪内容，排除已忽略与语料 PDF。

用途：在 git add 之前先把"将要提交什么"看清楚，避免把语料或大文件带进去。
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent

# 开源价值高、体积可控的目录
INTENDED = ['zhilian', 'tests', 'packaging', 'docs', 'deploy', 'web', 'site', 'scripts',
            '答辩评测', 'PPT交付包_知链']
# 只收录这些中小体积扩展名（报告与数据），排除语料与二进制
KEEP_EXT = {'.py', '.md', '.json', '.jsonl', '.csv', '.txt', '.sh', '.ps1', '.bat',
            '.yml', '.yaml', '.toml', '.cfg', '.ini', '.html', '.css', '.js', '.svg',
            '.png', '.xlsx', '.docx', '.pptx'}
# 明确排除：体积大或不该进仓库的
EXCLUDE_NAMES = {'size:big'}
MAX_BYTES = 2 * 1024 * 1024          # 单文件 2MB 上限


def git(*a) -> str:
    return subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', errors='replace').stdout


def is_ignored(p: Path) -> bool:
    r = subprocess.run(['git', 'check-ignore', '--no-index', str(p.relative_to(ROOT))],
                       cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    return r.returncode == 0


def main() -> int:
    status = [l for l in git('status', '--porcelain').splitlines() if l.strip()]
    cand: list[tuple[str, int]] = []
    skipped_ignored = 0
    skipped_big = 0
    for line in status:
        code, path = line[:2].strip(), line[3:].strip('"')
        p = ROOT / path
        if p.is_dir():
            for f in p.rglob('*'):
                if not f.is_file():
                    continue
                rel = f.relative_to(ROOT)
                if is_ignored(f):
                    skipped_ignored += 1
                    continue
                if f.suffix.lower() not in KEEP_EXT:
                    skipped_ignored += 1
                    continue
                if f.stat().st_size > MAX_BYTES:
                    skipped_big += 1
                    continue
                cand.append((str(rel), f.stat().st_size))
        elif p.is_file():
            if is_ignored(p):
                skipped_ignored += 1
                continue
            if p.stat().st_size > MAX_BYTES:
                skipped_big += 1
                continue
            cand.append((path, p.stat().st_size))

    print(f'候选文件 {len(cand)} 个 · 合计 {sum(s for _, s in cand)/1024/1024:.1f} MB')
    print(f'  因被忽略/类型不符跳过 {skipped_ignored} 个；因超过 2MB 跳过 {skipped_big} 个')
    print()
    by_top = defaultdict(lambda: [0, 0])
    for path, s in cand:
        top = path.split('/')[0] if '/' in path else '(根目录)'
        by_top[top][0] += 1
        by_top[top][1] += s
    print(f'{"目录":28} {"文件":>6} {"体积":>10}')
    for k, (n, s) in sorted(by_top.items(), key=lambda kv: -kv[1][1]):
        print(f'  {k:26} {n:>6} {s/1024/1024:>9.2f} MB')
    print()
    print('=== 体积最大的 10 个候选 ===')
    for path, s in sorted(cand, key=lambda x: -x[1])[:10]:
        print(f'  {s/1024/1024:>7.2f} MB  {path}')
    print()
    print('=== 按扩展名 ===')
    cnt = Counter(Path(p).suffix.lower() or '(无)' for p, _ in cand)
    for k, v in cnt.most_common(14):
        print(f'  {k:8} {v:>5}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
