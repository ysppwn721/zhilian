"""署名一致性核对：软件名称 / 版本号 / 权利人 三项在全部鉴别材料中是否统一。

官方要求：程序和文档中出现的权利人署名、软件名称及软件版本号，
应当与其他申请文件相应内容一致。
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
NAME = '知链跨文档结论验证与增量修复软件'
VERSION = '0.2.1'
OWNER = '林子钧'
STALE = ['V0.2.1', 'v0.2.1', 'V 0.2.1']

FILES = ['源程序_前30页和后30页.pdf', '文档_用户手册.pdf',
         '文档_程序设计说明书.pdf', '软件登记信息表.pdf']
# 鉴别材料（提交件）必须有页眉页脚署名；登记信息表是工作底稿，
# 只需文中出现正确的名称/版本/权利人，不要求页脚格式。
IDENTIFYING = {'源程序_前30页和后30页.pdf', '文档_用户手册.pdf', '文档_程序设计说明书.pdf'}

print(f'申请文件口径：软件名称 = {NAME}')
print(f'              版本号   = {VERSION}（不带 V）')
print(f'              权利人   = {OWNER}')
print()
bad = []
for n in FILES:
    p = DEST / n
    if not p.is_file():
        print(f'  [缺失] {n}')
        bad.append(n)
        continue
    doc = pymupdf.open(p)
    first = (doc[0].get_text('text') or '').splitlines()
    all_text = ''.join(page.get_text('text') for page in doc)

    header = next((l for l in first if NAME in l), '')
    footer = next((l for l in first if '著作权人：' in l), '')
    head_ok = NAME in header and VERSION in header
    stale = [s for s in STALE if s in all_text]

    if n in IDENTIFYING:
        foot_ok = NAME in footer and VERSION in footer and OWNER in footer
        ok = head_ok and foot_ok and not stale
        kind = '鉴别材料'
    else:
        # 工作底稿：校验名称/版本/权利人确实出现在文中，且与申请口径一致
        foot_ok = (NAME in all_text and VERSION in all_text and OWNER in all_text)
        ok = head_ok and foot_ok and not stale
        kind = '工作底稿'
    n_owner = all_text.count(OWNER)

    print(f'  {"OK " if ok else "!! "} {n}  [{kind}]')
    print(f'       页眉: {header[:76] or "（未找到）"}')
    if n in IDENTIFYING:
        print(f'       页脚: {footer[:76] or "（未找到）"}')
    print(f'       名称={NAME in all_text} 版本={VERSION in all_text} '
          f'权利人={OWNER in all_text}（出现 {n_owner} 次） 旧版本号残留={stale or "无"}')
    if not ok:
        bad.append(n)
    doc.close()

print()
if bad:
    print(f'  ✗ 不一致: {bad}')
else:
    print('  ✓ 三项（软件名称 / 版本号 / 权利人）在全部材料中一致，无旧版本号残留')
sys.exit(1 if bad else 0)
