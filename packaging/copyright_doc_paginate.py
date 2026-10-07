"""把登记文档按"每页不少于 30 行"分页，并生成连续前 30 页 / 后 30 页。

规则
----
- 文档每页不少于 30 行
- 提交连续前 30 页 + 连续后 30 页；不足 60 页则提交全部
- 页眉标注软件名称、版本与文档名；页脚标注页码

用法：
    python packaging/copyright_doc_paginate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'docs' / '软著登记'
SOFTWARE = '知链跨文档结论验证与增量修复软件'
VERSION = 'V0.2.1'
LINES_PER_PAGE = 30
PAGES = 30

DOCS = [
    ('文档_用户手册.md', '用户手册'),
    ('文档_程序设计说明书.md', '程序设计说明书'),
]


def strip_noise(lines: list[str]) -> list[str]:
    """压掉纯空行，保证每页达到 30 行的下限。"""
    return [l for l in lines if l.strip()]


def balance_pages(stream: list[str], min_per_page: int) -> tuple[list[list[str]], int]:
    """分页并保证**每一页（含末页）都不少于 min_per_page 行**。

    数学约束
    --------
    把 N 行分进 P 页且每页 ≥ M 行，是可行的当且仅当 `N >= P * M` 且 `P = ceil(N/M)`。
    取 P = ceil(N/M) 时，`P*M >= N` 恒成立（差值为 M - (N mod M)）。
    因此做法是：先按 M 行/页算出最少的页数 P，再把这 N 行**尽量均匀**分摊到 P 页，
    每页取 `floor(N/P)` 或 `floor(N/P)+1` 行。

    反例（实测踩过）：若取每页 M 行、并把余数摊到各页而不检查基数，
    当 `N/P == M - 1` 时会出现 29 行的页（N=507, M=30, P=17 → 507/17=29.8 → 每页 29 行），
    低于下限。上取整分配（每页 base 或 base+1，且 base = floor(N/P)）可保证
    `base >= M - 1`，配合余数补 1 后全部 ≥ M。
    """
    total_lines = len(stream)
    if total_lines == 0:
        return [], 0
    n_pages = max(1, (total_lines + min_per_page - 1) // min_per_page)
    # 均匀分配：base 与 base+1 混合，base = floor(N/P)
    base, extra = divmod(total_lines, n_pages)
    while base < min_per_page:
        # 理论上不会进入：P = ceil(N/M) 时 floor(N/P) >= M - 1。
        # 但若上游改了 M 或 N 的口径，这里兜底收敛，避免静默产出不合格页。
        n_pages = max(1, n_pages - 1)
        base, extra = divmod(total_lines, n_pages)
    pages: list[list[str]] = []
    pos = 0
    for i in range(n_pages):
        take = base + (1 if i < extra else 0)
        pages.append(stream[pos:pos + take])
        pos += take
    return pages, n_pages


def paginate(stream: list[str], header: str) -> tuple[list[str], int]:
    pages, total = balance_pages(stream, LINES_PER_PAGE)
    out: list[str] = []
    for i, chunk in enumerate(pages):
        out.append(f'—— 第 {i + 1} 页 / 共 {total} 页 | {header} ——')
        out.extend(chunk)
    return out, total


def main() -> int:
    for name, label in DOCS:
        src = DEST / name
        if not src.is_file():
            print(f'  跳过（不存在）: {name}')
            continue
        raw = src.read_text(encoding='utf-8', errors='replace').splitlines()
        body = strip_noise(raw)
        header = f'{SOFTWARE} {VERSION} · {label}'
        full, total = paginate(body, header)

        stem = name.replace('.md', '')
        if total < 2 * PAGES:
            print(f'{label}: {len(body)} 行 → {total} 页（不足 60 页，提交全文）')
            (DEST / f'{stem}_全文.txt').write_text('\n'.join(full), encoding='utf-8')
            continue

        head, _ = paginate(body[:PAGES * LINES_PER_PAGE], f'{header}（前 30 页）')
        tail, _ = paginate(body[-PAGES * LINES_PER_PAGE:], f'{header}（后 30 页）')
        (DEST / f'{stem}_前30页.txt').write_text('\n'.join(head), encoding='utf-8')
        (DEST / f'{stem}_后30页.txt').write_text('\n'.join(tail), encoding='utf-8')
        (DEST / f'{stem}_全文_供核对.txt').write_text('\n'.join(full), encoding='utf-8')
        print(f'{label}: {len(body)} 行 → {total} 页'
              f'（前 30 页 / 后 30 页已生成）')

    print()
    print('=== 产物核对 ===')
    for p in sorted(DEST.glob('文档_*.txt')):
        lines = p.read_text(encoding='utf-8').splitlines()
        pages = sum(1 for l in lines if l.startswith('—— 第'))
        # 抽查第一页正文行数
        idx = [i for i, l in enumerate(lines) if l.startswith('—— 第')]
        first_page_body = (idx[1] - idx[0] - 2) if len(idx) > 1 else len(lines) - 1
        ok = '✓' if first_page_body >= LINES_PER_PAGE else '✗'
        print(f'  {ok} {p.name:40} {pages:>3} 页  首页正文 {first_page_body} 行'
              f'  {p.stat().st_size/1024:>7.1f} KB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
