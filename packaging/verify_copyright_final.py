"""校验最终提交的软著 PDF（合并版源程序 + 文档）。

登记要求：
  源程序 前 30 页 + 后 30 页，每页不少于 50 行
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

DEST = Path(__file__).resolve().parent.parent / 'docs' / '软著登记' / 'PDF'
PREFIX = '知链跨文档结论验证'

SPECS = [
    ('源程序_前30页和后30页.pdf', 50, 60),
    ('文档_用户手册.pdf', 30, None),
    ('文档_程序设计说明书.pdf', 30, None),
    ('软件登记信息表.pdf', 30, None),
]

bad = []
print('=== 最终提交件校验 ===')
for name, need_lines, need_pages in SPECS:
    p = DEST / name
    if not p.is_file():
        print(f'  [缺失] {name}')
        bad.append(name)
        continue
    doc = pymupdf.open(p)
    per_page = []
    for page in doc:
        lines = (page.get_text('text') or '').splitlines()
        body = [l for l in lines if l.strip() and '页 / 共' not in l
                and not l.strip().startswith(PREFIX)]
        per_page.append(len(body))
    fonts = sorted({f[3] for page in doc for f in page.get_fonts()})
    doc.close()

    ok_lines = all(c >= need_lines for c in per_page)
    ok_pages = (need_pages is None) or (len(per_page) == need_pages)
    flag = 'OK ' if (ok_lines and ok_pages) else '!! '
    print(f'  {flag} {name}')
    print(f'       {len(per_page)} 页 · 每页 {min(per_page)}–{max(per_page)} 行 '
          f'(下限 {need_lines}) · 行数达标={ok_lines}'
          + (f' · 页数应为 {need_pages} → {ok_pages}' if need_pages else ''))
    print(f'       嵌入字体: {[f for f in fonts if "Sim" in f or "Hei" in f]}')
    if not (ok_lines and ok_pages):
        short = [(i + 1, c) for i, c in enumerate(per_page) if c < need_lines][:6]
        bad.append(f'{name} 行数不足页 {short}')
        if need_pages and len(per_page) != need_pages:
            bad.append(f'{name} 页数 {len(per_page)} ≠ {need_pages}')

print()
if bad:
    print(f'  ✗ 存在问题: {bad}')
else:
    print('  ✓ 全部提交件达到登记格式要求')
        # 位置标注：合并版里前 30 页与后 30 页的分界
    p = DEST / '源程序_前30页和后30页.pdf'
    print()
    print(f'  说明：{p.name} 为合并文件，共 60 页 = 前 30 页 + 后 30 页。')
sys.exit(1 if bad else 0)
