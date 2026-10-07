"""上传前的仓库体检：体积、不该提交的内容、工作区待收内容。"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent


def git(*args) -> str:
    r = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return r.stdout


print('=== 1) 已跟踪内容 ===')
tracked = [l for l in git('ls-files').splitlines() if l.strip()]
print(f'  文件数 {len(tracked)}')
sizes = []
for t in tracked:
    p = ROOT / t
    if p.is_file():
        sizes.append((p.stat().st_size, t))
total = sum(s for s, _ in sizes)
print(f'  合计 {total/1024/1024:.1f} MB')
print()
print('  最大的 10 个:')
for s, t in sorted(sizes, reverse=True)[:10]:
    print(f'    {s/1024/1024:>8.2f} MB  {t}')
print()

print('=== 2) 体积告警（>1MB 的已跟踪文件）===')
big = [(s, t) for s, t in sizes if s > 1024 * 1024]
if big:
    for s, t in sorted(big, reverse=True):
        print(f'  ⚠ {s/1024/1024:.2f} MB  {t}')
    print(f'  合计 {sum(s for s,_ in big)/1024/1024:.1f} MB —— 其中构建产物建议不进开源仓库')
else:
    print('  无')
print()

print('=== 3) 敏感路径是否被跟踪 ===')
SENSITIVE = ['.env', 'human_gold_api.env', 'scope_manual_overrides.json',
             'deploy/.env', 'zhilian.db', 'app.db']
for t in tracked:
    name = Path(t).name
    if name in ('.env', 'human_gold_api.env'):
        print(f'  ⚠ 已跟踪敏感文件: {t}')
print('  （上面无输出即安全）')
print()

print('=== 4) 工作区未跟踪但未忽略的目录（git add . 会收进来）===')
untracked = [l[3:].strip('"') for l in git('status', '--porcelain').splitlines()
             if l.startswith('??')]
dirs = Counter()
for u in untracked:
    p = ROOT / u
    if p.is_dir():
        dirs[u] = sum(1 for _ in p.rglob('*') if _.is_file())
    else:
        dirs[f'(文件) {Path(u).parent}'] += 1
for d, n in dirs.most_common(20):
    print(f'  {n:>6} 文件  {d}')
print()

print('=== 5) 是否含大体积素材（语料 PDF / 模型权重）在未跟踪目录 ===')
for name in ('答辩评测', 'models', 'output', 'dist', 'build', 'PPT', 'PPT交付包_知链', '交付物'):
    p = ROOT / name
    if not p.is_dir():
        continue
    tot = cnt = 0
    for f in p.rglob('*'):
        if f.is_file():
            tot += f.stat().st_size
            cnt += 1
    ignored = subprocess.run(['git', 'check-ignore', name], cwd=ROOT,
                             capture_output=True, text=True).returncode == 0
    print(f'  {name:24} {cnt:>6} 文件  {tot/1024/1024:>9.1f} MB  '
          f'{"已忽略" if ignored else "★ 未忽略"}')
