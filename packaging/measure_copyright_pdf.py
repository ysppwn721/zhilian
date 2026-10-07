"""量真实 PDF 的每页行数，反推每个材料需要的排版行数下限。

不复用生成器自己的账（它数的是"排版行"，与实际提取出的文本行不完全一致），
而是用 pymupdf 打开成品 PDF 逐页测量。
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
SOFTWARE_PREFIX = '知链跨文档结论验证'


def measure(path: Path) -> dict:
    doc = pymupdf.open(path)
    per_page = []
    for page in doc:
        text = page.get_text('text') or ''
        raw = text.splitlines()
        # 页眉：含"页 / 共"；页脚：含软件全名
        body = [l for l in raw
                if l.strip()
                and '页 / 共' not in l
                and not l.strip().startswith(SOFTWARE_PREFIX)]
        per_page.append(len(body))
    doc.close()
    return {'pages': len(per_page), 'min': min(per_page), 'max': max(per_page),
            'per_page': per_page}


print('=== 逐个 PDF 实测每页行数 ===')
for name, need in (('源程序_前30页.pdf', 50), ('源程序_后30页.pdf', 50),
                   ('文档_用户手册.pdf', 30), ('文档_程序设计说明书.pdf', 30),
                   ('软件登记信息表.pdf', 30)):
    p = DEST / name
    if not p.is_file():
        print(f'  [缺失] {name}')
        continue
    m = measure(p)
    ok = m['min'] >= need
    print(f'  {"OK " if ok else "!! "} {name:32} {m["pages"]:>3} 页 · '
          f'每页 {m["min"]}–{m["max"]} 行 (下限 {need}) 达标={ok}')
    if not ok:
        short = [(i + 1, c) for i, c in enumerate(m['per_page']) if c < need]
        print(f'       不足的页: {short[:8]}')
print()
print('=== 每页行数分布（源程序前 30 页）===')
m = measure(DEST / '源程序_前30页.pdf')
print(f'  {m["per_page"]}')
