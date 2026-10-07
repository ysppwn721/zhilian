"""分析「大体积但不该进开源仓库」的构成，并给出精确的 .gitignore 规则。

目标：语料 PDF / 模型权重 / 构建产物 不进仓库；
      报告、文档、脚本、评测 JSON 等小体积产物可以进。
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
BIG_EXT = {'.pdf': '语料 PDF', '.docx': '文档', '.xlsx': '表格', '.zip': '压缩包',
           '.tar.gz': '压缩包', '.onnx': '模型', '.bin': '模型', '.safetensors': '模型',
           '.png': '图片', '.svg': '矢量图', '.csv': '数据表', '.jsonl': '数据', '.json': '数据'}


def scan(name: str):
    p = ROOT / name
    if not p.is_dir():
        return
    by_ext = defaultdict(lambda: [0, 0])
    total = 0
    for f in p.rglob('*'):
        if not f.is_file():
            continue
        s = f.stat().st_size
        total += s
        ext = f.suffix.lower()
        if ext in ('.gz',) and f.name.endswith('.tar.gz'):
            ext = '.tar.gz'
        key = BIG_EXT.get(ext, ext or '(无扩展名)')
        by_ext[key][0] += 1
        by_ext[key][1] += s
    print(f'=== {name}  合计 {total/1024/1024:.1f} MB ===')
    for k, (n, s) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:8]:
        print(f'   {k:12} {n:>6} 文件  {s/1024/1024:>9.1f} MB')
    print()
    return by_ext


for name in ('答辩评测', '交付物', 'output', 'PPT', 'PPT交付包_知链'):
    scan(name)

print('=== 答辩评测 下的子目录体积（找出语料所在）===')
p = ROOT / '答辩评测'
subs = []
for d in p.iterdir():
    if d.is_dir():
        s = sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
        subs.append((s, d.name))
for s, n in sorted(subs, reverse=True)[:12]:
    print(f'   {s/1024/1024:>9.1f} MB  {n}')
print()

print('=== 答辩评测 下的大文件（>5MB）===')
big = [(f.stat().st_size, f) for f in p.rglob('*') if f.is_file() and f.stat().st_size > 5 * 1024 * 1024]
print(f'   共 {len(big)} 个，合计 {sum(s for s,_ in big)/1024/1024:.0f} MB')
for s, f in sorted(big, reverse=True)[:6]:
    print(f'   {s/1024/1024:>8.1f} MB  {f.relative_to(ROOT)}')
