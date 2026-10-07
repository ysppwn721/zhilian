"""校验生成的软著 PDF：页数、每页行数、页眉、字体嵌入。

登记要求：
  源程序 每页不少于 50 行
  文档   每页不少于 30 行
"""
from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'docs' / '软著登记' / 'PDF'

SPECS = {
    '源程序_前30页.pdf': 50,
    '源程序_后30页.pdf': 50,
    '文档_用户手册.pdf': 30,
    '文档_程序设计说明书.pdf': 30,
    '软件登记信息表.pdf': 30,
}

for name, minimum in SPECS.items():
    p = DEST / name
    if not p.is_file():
        print(f'  [缺失] {name}')
        continue
    doc = pymupdf.open(p)
    counts = []
    for page in doc:
        text = page.get_text('text') or ''
        lines = [l for l in text.splitlines() if l.strip()]
        # 去掉页眉行（含"第 N 页"）与页脚（含软件名）
        body = [l for l in lines
                if '页 / 共' not in l and not l.strip().startswith('知链跨文档结论验证')]
        counts.append(len(body))
    header_ok = '页 / 共' in (doc[0].get_text('text') or '')
    fonts = set()
    for page in doc:
        for f in page.get_fonts():
            fonts.add(f[3])
    doc.close()
    ok = all(c >= minimum for c in counts)
    print(f'  {"OK " if ok else "!! "} {name}')
    print(f'       {len(counts)} 页 · 每页正文 {min(counts)}–{max(counts)} 行 '
          f'(下限 {minimum}) · 全部达标={ok}')
    print(f'       页眉含页码={header_ok} · 嵌入字体={sorted(fonts)}')

print()
print('=== 结论 ===')
bad = []
for name, minimum in SPECS.items():
    p = DEST / name
    if not p.is_file():
        bad.append(name)
        continue
    doc = pymupdf.open(p)
    for i, page in enumerate(doc, 1):
        lines = [l for l in (page.get_text('text') or '').splitlines() if l.strip()]
        body = [l for l in lines if '页 / 共' not in l
                and not l.strip().startswith('知链跨文档结论验证')]
        if len(body) < minimum:
            bad.append(f'{name} 第 {i} 页仅 {len(body)} 行')
            break
    doc.close()
print('  ✓ 全部 PDF 满足每页行数下限' if not bad else f'  ✗ 问题: {bad[:5]}')
