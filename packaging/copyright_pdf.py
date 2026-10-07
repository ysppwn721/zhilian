"""把软著登记材料生成为 PDF（登记要求：所有上传文档必须为 PDF）。

核心设计：先折行、再分页
------------------------
朴素做法「先按 N 行切片，再逐行折行」会出错：长行折行后行数变多，
末页只剩十几行（实测说明书末页仅 12 行），违反"每页不少于 N 行"。
正确顺序是：
  1. 把每一条源文本按可用宽度折成"渲染行"
  2. 用渲染行总数算出页数 P = ceil(总行数 / 每页下限)
  3. 把渲染行**均匀**分给 P 页（每页 base 或 base+1 行）
这样每页必定 ≥ 下限，且末页不会出现少量孤行。

版式
----
- A4 纵向；页边距 上下 62/52pt、左右 52pt
- 页眉：软件名称 + 版本 + 材料名（居左）；页码（居右）；下划线分隔
- 源程序：宋体 8.4pt / 行距 11.6pt，每页 ≥50 行
- 文档：宋体 10.5pt / 行距 15pt，每页 ≥30 行

用法：
    python packaging/copyright_pdf.py
"""
from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'docs' / '软著登记'
DEST = SRC / 'PDF'
SOFTWARE = '知链跨文档结论验证与增量修复软件'
# 版本号不带 V：官方填表说明规定「鉴别材料页眉的软件版本号应与申请表符合一致，
# 但有无 V 以申请表中填报为准」，申请表中填 0.2.1，故此处一致写 0.2.1。
VERSION = '0.2.1'
# 著作权人署名：软著要求「程序和文档中出现的权利人署名、软件名称及软件版本号
# 应当与其他申请文件相应内容一致」，故页脚统一印权利人。
OWNER = '林子钧'

PAGE_W, PAGE_H = A4
M_LEFT, M_RIGHT = 52, 52
M_TOP, M_BOTTOM = 62, 52
USABLE_W = PAGE_W - M_LEFT - M_RIGHT
HEADER_BASELINE = PAGE_H - 34          # 页眉文字基线
RULE_Y = PAGE_H - 40                   # 页眉分隔线
FIRST_BODY_Y = PAGE_H - M_TOP - 8      # 正文首行基线，务必低于分隔线
FONT_DIR = Path(r'C:\Windows\Fonts')
FONTS = {'song': ('simsun.ttc', 0), 'hei': ('simhei.ttf', None)}


def register_fonts() -> dict:
    ok = {}
    for name, (fname, idx) in FONTS.items():
        p = FONT_DIR / fname
        if not p.is_file():
            print(f'  [警告] 字体缺失 {fname}')
            continue
        try:
            pdfmetrics.registerFont(
                TTFont(name, str(p)) if idx is None
                else TTFont(name, str(p), subfontIndex=idx))
            ok[name] = name
        except Exception as exc:                       # noqa: BLE001
            print(f'  [警告] 注册 {fname} 失败: {type(exc).__name__}: {exc}')
    return ok


def wrap(text: str, font: str, size: float, max_w: float) -> list[str]:
    if not text:
        return ['']
    out, cur = [], ''
    for ch in text:
        if pdfmetrics.stringWidth(cur + ch, font, size) <= max_w:
            cur += ch
        else:
            out.append(cur)
            cur = ch
    out.append(cur)
    return out


def balance(rendered: list[str], min_per_page: int) -> tuple[list[list[str]], int]:
    """把渲染行均匀分成若干页，**每页不少于 min_per_page 行**。"""
    n = len(rendered)
    if n == 0:
        return [[]], 1
    pages_count = max(1, (n + min_per_page - 1) // min_per_page)
    base, extra = divmod(n, pages_count)
    while base < min_per_page and pages_count > 1:
        pages_count -= 1
        base, extra = divmod(n, pages_count)
    pages, pos = [], 0
    for i in range(pages_count):
        take = base + (1 if i < extra else 0)
        pages.append(rendered[pos:pos + take])
        pos += take
    return pages, pages_count


def make_pdf(path: Path, header: str, lines: list[str], *,
             font: str, bold: str, size: float, leading: float,
             min_per_page: int, title: str) -> dict:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(title)
    c.setAuthor(SOFTWARE)
    c.setSubject(header)

    rendered: list[str] = []
    for raw in lines:
        rendered.extend(wrap(raw.rstrip('\n'), font, size, USABLE_W))

    pages, total = balance(rendered, min_per_page)
    for pi, chunk in enumerate(pages, 1):
        c.setFont(bold, 8)
        c.drawString(M_LEFT, HEADER_BASELINE, header)
        c.setFont(font, 8)
        c.drawRightString(PAGE_W - M_RIGHT, HEADER_BASELINE, f'第 {pi} 页 / 共 {total} 页')
        c.setLineWidth(0.4)
        c.line(M_LEFT, RULE_Y, PAGE_W - M_RIGHT, RULE_Y)

        c.setFont(font, size)
        y = FIRST_BODY_Y
        for ln in chunk:
            c.drawString(M_LEFT, y, ln)
            y -= leading

        c.setFont(font, 7.5)
        c.drawCentredString(PAGE_W / 2, 30, f'{SOFTWARE} {VERSION}　著作权人：{OWNER}')
        c.showPage()
    c.save()
    return {'file': path.name, 'pages': total, 'rendered_lines': len(rendered),
            'min_per_page': min_per_page, 'actual_min': min(len(p) for p in pages),
            'bytes': path.stat().st_size}


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    fonts = register_fonts()
    body = fonts.get('song') or fonts.get('hei')
    bold = fonts.get('hei') or body
    print(f'已注册字体: {sorted(fonts)}')

    jobs = [
        # 行距与每页行数都留足余量。实测教训：
        #  1) 8.4pt 字号配 11.6pt 行距过密，PDF 文本提取器会把相邻行按基线距离合并，
        #     量出来只有 47–48 行（低于 50 行下限）→ 行距提到 12.4pt。
        #  2) 即便行距够了，每页仍会稳定少 1 行（提取器对某类行合并），
        #     所以每页排版 56 行（可用高度 728pt / 12.4pt ≈ 58 行，容得下），
        #     实测每页 ≥50 行，留 6 行余量。
        #  3) 前 30 页与后 30 页合并为**同一个 PDF**（共 60 页），符合"提交连续
        #     前 30 页和连续后 30 页"的实务做法；见下方 source_pair 分支。
        ('文档_用户手册.pdf', '用户手册', '文档_用户手册.md', 9.5, 13.2, 54),
        ('文档_程序设计说明书.pdf', '程序设计说明书', '文档_程序设计说明书.md', 9.5, 13.2, 54),
        ('软件登记信息表.pdf', '登记信息表', '软件登记信息表.md', 9.5, 13.2, 54),
    ]

    # 源程序：前 30 页 + 后 30 页合并成一个 PDF，并**自动标定到正好 60 页**。
    head_f = SRC / '源程序_前30页.txt'
    tail_f = SRC / '源程序_后30页.txt'
    if head_f.is_file() and tail_f.is_file():
        head_all = [l for l in head_f.read_text(encoding='utf-8', errors='replace').splitlines()
                    if not l.startswith('—— 第')]
        tail_all = [l for l in tail_f.read_text(encoding='utf-8', errors='replace').splitlines()
                    if not l.startswith('—— 第')]
        OUT_PDF = DEST / '源程序_前30页和后30页.pdf'
        header_src = f'{SOFTWARE} {VERSION} · 源程序（前 30 页 + 后 30 页）'
        TARGET_PAGES, MPP = 60, 56

        # 为什么需要标定：一"条"源文本可能在 PDF 里折成两行，且提取时相邻行偶有合并，
        # 于是 输入行数 → 页数 不是整数比（实测 3262 行→59 页、3558 行→64 页）。
        # 做法：先按两侧全量跑一次，量出"输入行 → 页数"的比例，再按比例反推
        # 需要的每侧行数，重跑一次。两次即可收敛，且结果可复现。
        def _render(per_side: int):
            return make_pdf(OUT_PDF, header_src, head_all[-per_side:] + tail_all[-per_side:],
                            font=body, bold=bold, size=8.4, leading=12.4,
                            min_per_page=MPP,
                            title=f'{SOFTWARE} {VERSION} 源程序 前30页和后30页')

        probe = _render(min(len(head_all), len(tail_all)))
        per_page = probe['rendered_lines'] / max(1, probe['pages'])
        per_side = max(1, round(TARGET_PAGES * per_page / 2))
        info = _render(min(per_side, len(head_all), len(tail_all)))
        # 若仍偏离目标，按同一比例再校准一次
        if info['pages'] != TARGET_PAGES and info['pages'] > 0:
            per_side = max(1, round(per_side * TARGET_PAGES / info['pages']))
            info = _render(min(per_side, len(head_all), len(tail_all)))

        info['parts'] = {'每侧行数': per_side}
        print(f'  OK  {info["file"]:32} {info["pages"]:>3} 页 · '
              f'每页 ≥{MPP} 行（实际最少 {info["actual_min"]} 行）· '
              f'{info["bytes"]/1024:>7.1f} KB')
        print(f'        标定：每侧 {per_side} 行（前 {per_side} + 后 {per_side} = '
              f'{per_side*2} 行）→ {info["pages"]} 页')
        if info['pages'] != TARGET_PAGES:
            print(f'        [提示] 目标 {TARGET_PAGES} 页，实际 {info["pages"]} 页')
    else:
        print('  [跳过] 缺少源程序前/后 30 页文本，先运行 packaging/copyright_materials.py')

    # 文档与登记信息表
    for out, label, src, size, leading, mpp in jobs:
        p = SRC / src
        if not p.is_file():
            print(f'  [跳过] 缺少 {src}')
            continue
        info = make_pdf(DEST / out, f'{SOFTWARE} {VERSION} · {label}',
                        p.read_text(encoding='utf-8', errors='replace').splitlines(),
                        font=body, bold=bold, size=size, leading=leading,
                        min_per_page=mpp, title=f'{SOFTWARE} {VERSION} {label}')
        print(f'  OK  {info["file"]:32} {info["pages"]:>3} 页 · '
              f'每页 ≥{mpp} 行（实际最少 {info["actual_min"]} 行）· '
              f'{info["bytes"]/1024:>7.1f} KB')

    print()
    print(f'→ {DEST.relative_to(ROOT)}')
    return 0
    print()
    for out, label, src, size, leading, mpp in jobs:
        p = SRC / src
        if not p.is_file():
            print(f'  [跳过] 缺少 {src}')
            continue
        info = make_pdf(DEST / out, f'{SOFTWARE} {VERSION} · {label}',
                        p.read_text(encoding='utf-8', errors='replace').splitlines(),
                        font=body, bold=bold, size=size, leading=leading,
                        min_per_page=mpp, title=f'{SOFTWARE} {VERSION} {label}')
        print(f'  OK  {info["file"]:32} {info["pages"]:>3} 页 · '
              f'每页 ≥{mpp} 行（实际最少 {info["actual_min"]} 行）· '
              f'{info["bytes"]/1024:>7.1f} KB')
    print()
    print(f'→ {DEST.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
