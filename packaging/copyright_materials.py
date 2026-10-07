"""生成软件著作权登记的「源程序」材料：连续前 30 页 + 连续后 30 页。

规则（按《计算机软件著作权登记办法》与常见实务要求）
--------------------------------------------------
- 每页不少于 50 行（本脚本压掉纯空行后按 50 行/页分页，确保达标）
- 提交连续的前 30 页与连续的后 30 页；若整体不到 60 页则提交全部
- 页眉标注软件名称与版本，页脚标注页码；行内保留原始缩进
- 按"功能主次"排序：主程序与核心模块在前，测试/工具在后
  （登记办法允许按开发时间或功能主次自定义排序）

输出
----
  docs/软著登记/源程序_前30页.txt
  docs/软著登记/源程序_后30页.txt
  docs/软著登记/源程序_全文_供核对.txt
  docs/软著登记/源程序_页码索引.md
"""
from __future__ import annotations

import json
import re
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
# 版本号不带 V，与申请表填报一致（官方：有无 V 以申请表为准）
VERSION = '0.2.1'
OWNER = '林子钧'
LINES_PER_PAGE = 50
PAGES = 30
# 交给 PDF 生成器的每页行数。
# 为什么不用 LINES_PER_PAGE：登记要求"每页不少于 50 行"，而 PDF 排版的每页行数
# 必须 ≥50 且总页数正好 30。若按 50 行/页切 1500 行，PDF 侧折行后只够排 28 页；
# 这里按 60 行/页切 1800 行交给 PDF，由 packaging/copyright_pdf.py 重新分页，
# 实测输出正好 30 页、每页 50 行以上。
SOURCE_LINES_FOR_PDF = 56
# 交给 PDF 的**总行数**。
# 目标：合并前 30 页 + 后 30 页后，PDF 正好 60 页（每页 ≥50 行）。
# 实测比例约为「56 排版行 → 55.3 提取行」，故 60 页需约 60×56×(56/55.3) ≈ 3400 行。
# 取 30 页 × 57 行 = 1710 行/侧，两侧合计 3420 行 → 实测 60 页。
SOURCE_SLICE_LINES = 30 * 57

# 按功能主次排序：主入口 → 核心引擎 → 存储 → 办公文档 → 界面 → 智能体/模型 → 工具 → 打包测试
# 范围：项目自身的全部源程序（zhilian 包 + 根目录入口 + 测试 + 打包工具）。
# 注意不要只收 ORDER 里列到的文件——实测那样"后 30 页"会只剩打包与测试脚本，
# 不能体现核心功能；登记材料应让前后 30 页都覆盖实质业务代码。
ORDER = [
    # 1. 主入口与运行框架
    'run.py', 'zhilian/__init__.py', 'zhilian/app.py',
    # 2. 核心：事实与断言引擎
    'zhilian/engine.py', 'zhilian/store.py',
    # 3. 关联图与诊断
    'zhilian/graph.py', 'zhilian/diagnose.py',
    # 4. Office 文档读写与写回
    'zhilian/office.py', 'zhilian/pdf_writeback.py', 'zhilian/pdf_office_export.py',
    # 5. PDF 解析与导出
    'zhilian/pdf_ingest.py', 'zhilian/pdf_revised_export.py', 'zhilian/pdf_visual_docx.py',
    'zhilian/pdf_overlay_experiment.py', 'zhilian/ocr.py',
    # 6. 智能体与人工复核闭环
    'zhilian/agent.py', 'zhilian/review.py', 'zhilian/tools.py',
    # 7. 模型接入（可选）
    'zhilian/llm.py', 'zhilian/reranker.py', 'zhilian/document_profile.py',
    # 8. 报告、配额与集成
    'zhilian/report.py', 'zhilian/quota.py', 'zhilian/mcp_server.py', 'zhilian/demo.py',
]

SKIP_DIRS = {'.venv', '.git', 'node_modules', 'site-packages', 'dist', 'build',
             '__pycache__', '.train-cuda-venv', '.build-venv', 'output', '答辩评测',
             '交付物', 'PPT', 'PPT交付包_知链', 'models', '华北五省设计',
             '.pytest-run', 'pytest-of-Administrator', '.ppt_build', '.ppt-edit-build',
             'tmp', 'artifacts', 'downloads-large'}


def collect_files() -> list[Path]:
    """收集源程序文件，按 ORDER 优先、其余按路径排序。"""
    picked: list[Path] = []
    seen: set[Path] = set()

    for rel in ORDER:
        p = ROOT / rel
        if p.is_file():
            picked.append(p)
            seen.add(p)

    rest: list[Path] = []
    for p in ROOT.rglob('*'):
        if not p.is_file() or p.suffix.lower() != '.py':
            continue
        rel = p.relative_to(ROOT)
        if set(rel.parts[:-1]) & SKIP_DIRS:
            continue
        if p.stat().st_size > 1_000_000:
            continue
        if p in seen:
            continue
        # 收集项目自身源程序：zhilian 包、根目录入口、tests、packaging 下的生产脚本。
        # 排除 site/ web/ deploy/ 等与源程序无关的目录，以及一次性实验脚本。
        top = rel.parts[0] if len(rel.parts) > 1 else '(root)'
        if top in ('packaging', 'tests', 'scripts'):
            rest.append(p)
        elif top == '(root)':
            rest.append(p)
    picked.extend(sorted(rest, key=lambda x: str(x.relative_to(ROOT))))
    return picked


def strip_noise(lines: list[str]) -> list[str]:
    """压掉纯空行（保证每页 ≥50 行），保留缩进与注释。"""
    return [l for l in lines if l.strip()]


def render(files: list[Path], header: str) -> tuple[list[str], list[dict]]:
    """把文件展开成带来源标注的行流，再按页切分。"""
    stream: list[str] = []
    index: list[dict] = []
    for p in files:
        rel = str(p.relative_to(ROOT)).replace('\\', '/')
        try:
            raw = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        body = strip_noise(raw)
        if not body:
            continue
        start_line = len(stream) + 1
        stream.append(f'// ===== 文件：{rel} =====')
        stream.extend(body)
        index.append({'file': rel, 'lines': len(body),
                      'stream_start': start_line, 'stream_end': len(stream)})
    return stream, index


def paginate(stream: list[str], header: str) -> tuple[list[str], int]:
    total_pages = (len(stream) + LINES_PER_PAGE - 1) // LINES_PER_PAGE
    out: list[str] = []
    for i in range(total_pages):
        chunk = stream[i * LINES_PER_PAGE:(i + 1) * LINES_PER_PAGE]
        out.append(f'—— 第 {i + 1} 页 / 共 {total_pages} 页 | {header} ——')
        out.extend(chunk)
        out.append('')
        out.append('')
    return out, total_pages


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    files = collect_files()
    print(f'源程序文件 {len(files)} 个')

    stream, index = render(files, '')
    print(f'压掉空行后共 {len(stream)} 行 → 每页 {LINES_PER_PAGE} 行，'
          f'折合 {(len(stream) + LINES_PER_PAGE - 1) // LINES_PER_PAGE} 页')

    if len(stream) < PAGES * LINES_PER_PAGE * 2:
        print('  程序整体不足 60 页 → 按规则提交全部源程序')
        full, _ = paginate(stream, f'{SOFTWARE} {VERSION}')
        (DEST / '源程序_全文.txt').write_text('\n'.join(full), encoding='utf-8')
        return 0

    head_pages, _ = paginate(stream[:SOURCE_SLICE_LINES],
                             f'{SOFTWARE} {VERSION}（前 30 页）')
    tail_pages, _ = paginate(stream[-SOURCE_SLICE_LINES:],
                             f'{SOFTWARE} {VERSION}（后 30 页）')
    full_pages, total = paginate(stream, f'{SOFTWARE} {VERSION}')

    (DEST / '源程序_前30页.txt').write_text('\n'.join(head_pages), encoding='utf-8')
    (DEST / '源程序_后30页.txt').write_text('\n'.join(tail_pages), encoding='utf-8')
    (DEST / '源程序_全文_供核对.txt').write_text('\n'.join(full_pages), encoding='utf-8')

    # 页码索引：说明前后 30 页各覆盖了哪些文件，便于答辩与核对
    def cover(offset: int, n: int) -> list[str]:
        lo, hi = offset + 1, offset + n * LINES_PER_PAGE
        return [f'{it["file"]}（{it["lines"]} 行）' for it in index
                if it['stream_end'] >= lo and it['stream_start'] <= hi]

    lines = [f'# 源程序页码索引', '',
             f'- 软件名称：{SOFTWARE}', f'- 版本号：{VERSION}',
             f'- 源程序文件数：{len(files)}',
             f'- 压空行后总行数：{len(stream)}',
             f'- 每页行数：{LINES_PER_PAGE}',
             f'- 总页数：{total}', '',
             '## 前 30 页覆盖的文件', '']
    lines += [f'- {x}' for x in cover(0, PAGES)]
    lines += ['', '## 后 30 页覆盖的文件', '']
    lines += [f'- {x}' for x in cover(len(stream) - PAGES * LINES_PER_PAGE, PAGES)]
    lines += ['', '## 排序说明', '',
              '排序按「功能主次」而非开发时间：主入口 → 事实引擎 → 存储 → 关联与诊断 →',
              'Office 写回 → PDF 解析导出 → 智能体与复核闭环 → 模型接入 → 报告 → 打包评测 → 测试。',
              '《计算机软件著作权登记办法》允许按开发时间或功能主次等自定义排序。', '']
    (DEST / '源程序_页码索引.md').write_text('\n'.join(lines), encoding='utf-8')

    print()
    for f in ('源程序_前30页.txt', '源程序_后30页.txt', '源程序_全文_供核对.txt',
              '源程序_页码索引.md'):
        p = DEST / f
        n = len(p.read_text(encoding='utf-8').splitlines())
        print(f'  {f:28} {n:>7} 行  {p.stat().st_size/1024:>8.1f} KB')
    print()
    print('前 30 页覆盖：')
    for x in cover(0, PAGES)[:12]:
        print(f'  - {x}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
