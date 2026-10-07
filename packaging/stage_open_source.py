"""按白名单暂存可开源内容，并给出暂存后的体积审计。

为什么用白名单而不是 `git add .`
--------------------------------
实测工作区里 `答辩评测/` 有 8068 MB 语料 PDF、`output/` 有 394 MB 运行时状态、
`交付物/` 有 1075 MB 安装包。`git add .` 即使有 .gitignore 兜底，也容易漏掉新增目录，
所以这里显式列出要开源的内容，其余一律不动。
"""
from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 5 * 1024 * 1024          # 单文件 5MB 上限，超过则跳过并报告

DIRS = ['zhilian', 'tests', 'packaging', 'docs', 'deploy', 'web', 'site', 'scripts',
        'third_party']
ROOT_FILES = ['.gitignore', 'README.md', 'requirements.txt', 'requirements-runtime.txt',
              'pytest.ini', 'start.sh', 'start.ps1', 'install.sh', 'setup.sh',
              'pyproject.toml', 'LICENSE', '.env.example']
# 评测报告与结论文档：小而关键，保留；语料与运行时状态排除
DOC_DIRS = ['答辩评测']
DOC_KEEP_EXT = {'.md', '.py', '.csv', '.json'}
DOC_EXCLUDE_SUBSTR = ('annual_reports_', 'cn_reports', 'public_data', 'public_reranker_pairs',
                      'text_anchors', '/.git/')


def git(*a, check=False):
    r = subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if check and r.returncode != 0:
        print(f'git {" ".join(a)} 失败: {r.stderr[:200]}', file=sys.stderr)
    return r


def ignored(rel: str) -> bool:
    return git('check-ignore', '--no-index', rel).returncode == 0


def collect() -> tuple[list[str], list[tuple[str, int]], list[tuple[str, int]]]:
    take: list[str] = []
    skipped_big: list[tuple[str, int]] = []
    skipped_ext: list[tuple[str, int]] = []

    def consider(p: Path, exts: set[str] | None = None):
        rel = str(p.relative_to(ROOT))
        if ignored(rel):
            return
        if exts is not None and p.suffix.lower() not in exts:
            return
        s = p.stat().st_size
        if s > MAX_BYTES:
            skipped_big.append((rel, s))
            return
        take.append(rel)

    for d in DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob('*'):
            if f.is_file():
                consider(f)

    for name in ROOT_FILES:
        p = ROOT / name
        if p.is_file():
            consider(p)

    for d in DOC_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob('*'):
            if not f.is_file():
                continue
            rel = str(f.relative_to(ROOT))
            if any(x in rel for x in DOC_EXCLUDE_SUBSTR):
                continue
            if f.suffix.lower() not in DOC_KEEP_EXT:
                skipped_ext.append((rel, f.stat().st_size))
                continue
            consider(f, DOC_KEEP_EXT)

    return take, skipped_big, skipped_ext


def main() -> int:
    take, skipped_big, skipped_ext = collect()
    total = sum((ROOT / t).stat().st_size for t in take)
    print(f'待暂存 {len(take)} 个文件 · {total/1024/1024:.1f} MB')
    by = defaultdict(lambda: [0, 0])
    for t in take:
        top = t.replace('\\', '/').split('/')[0]
        s = (ROOT / t).stat().st_size
        by[top][0] += 1
        by[top][1] += s
    for k, (n, s) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        print(f'   {k:22} {n:>5} 文件  {s/1024/1024:>8.2f} MB')
    print()
    if skipped_big:
        print(f'因超过 {MAX_BYTES/1024/1024:.0f}MB 跳过 {len(skipped_big)} 个:')
        for rel, s in sorted(skipped_big, key=lambda x: -x[1])[:8]:
            print(f'   {s/1024/1024:>7.2f} MB  {rel}')
        print()
    print(f'「答辩评测/」中因扩展名不符跳过 {len(skipped_ext)} 个（多为语料与运行时文件）')
    print()

    if '--apply' in sys.argv:
        batch = 400
        for i in range(0, len(take), batch):
            git('add', '--', *take[i:i + batch])
        staged = [l for l in git('diff', '--cached', '--name-only').stdout.splitlines() if l.strip()]
        print(f'已暂存 {len(staged)} 个文件')
        st = sum((ROOT / s).stat().st_size for s in staged if (ROOT / s).is_file())
        print(f'暂存体积 {st/1024/1024:.1f} MB')
    else:
        print('（未暂存；加 --apply 执行）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
