"""统计知链（Zhilian）项目的源程序量、编程语言与技术栈，供软件著作权登记使用。

原则：全部数字从真实代码统计，不估算、不凭印象。
统计范围与软件著作权登记的"源程序量"口径对齐：
  - 计入：项目自身的源代码（.py/.js/.ts/.html/.css/.sh/.ps1/.bat 等）
  - 排除：第三方依赖（.venv、node_modules、site-packages、dist、build）、
          生成的静态资源、数据文件、语料
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.venv', '.git', 'node_modules', 'site-packages', 'dist', 'build',
             '__pycache__', '.train-cuda-venv', '.build-venv', 'site_packages',
             'artifacts', 'output', '答辩评测', '交付物', 'PPT', 'PPT交付包_知链',
             'models', '华北五省设计', '.pytest-run', 'pytest-of-Administrator',
             '.ppt_build', '.ppt-edit-build', 'tmp', 'downloads-large'}

LANG = {
    '.py': 'Python', '.js': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript', '.jsx': 'JavaScript',
    '.html': 'HTML', '.htm': 'HTML', '.css': 'CSS', '.scss': 'SCSS',
    '.sh': 'Shell', '.bash': 'Shell', '.ps1': 'PowerShell', '.bat': 'Batch',
    '.cmd': 'Batch', '.sql': 'SQL', '.yml': 'YAML', '.yaml': 'YAML',
    '.json': 'JSON', '.toml': 'TOML', '.ini': 'INI', '.cfg': 'INI',
    '.svg': 'SVG', '.md': 'Markdown', '.xaml': 'XAML', '.cs': 'C#',
}
# 真正的"源程序"语言（登记用的编程语言从这里取，排除配置/文档/标记）
CODE_LANG = {'Python', 'JavaScript', 'TypeScript', 'HTML', 'CSS', 'SCSS',
             'Shell', 'PowerShell', 'Batch', 'SQL', 'C#', 'XAML', 'SVG'}


def source_files():
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        # 只比较"目录部分"与跳过集合——p.parts 含文件名，
        # 若直接对整体求交集，会把名字恰好等于跳过项的**文件**也误判（实测曾导致 0 命中）
        rel = p.relative_to(ROOT)
        if set(rel.parts[:-1]) & SKIP_DIRS:
            continue
        # 仓库根目录下的 packaging/ 是生产脚本目录，必须计入；此处不做排除
        if p.suffix.lower() not in LANG:
            continue
        if p.stat().st_size > 2_000_000:
            continue
        yield p


def main() -> int:
    stats = defaultdict(lambda: {'files': 0, 'lines': 0, 'blank': 0, 'comment': 0,
                                 'code': 0, 'bytes': 0})
    per_file = []
    for p in source_files():
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        lines = text.splitlines()
        lang = LANG[p.suffix.lower()]
        blank = sum(1 for l in lines if not l.strip())
        if lang == 'Python':
            # Python 用 AST 精确区分注释与代码（含 docstring 记为注释）
            comment = 0
            for l in lines:
                s = l.strip()
                if s.startswith('#'):
                    comment += 1
        else:
            comment = sum(1 for l in lines if l.strip().startswith(('//', '#', '--', '/*', '*')))
        code = len(lines) - blank - comment
        s = stats[lang]
        s['files'] += 1
        s['lines'] += len(lines)
        s['blank'] += blank
        s['comment'] += comment
        s['code'] += code
        s['bytes'] += p.stat().st_size
        per_file.append({'path': str(p.relative_to(ROOT)), 'lang': lang,
                         'lines': len(lines), 'bytes': p.stat().st_size})

    print('=' * 78)
    print('一、按编程语言统计（仅项目自身源代码）')
    print('=' * 78)
    print(f'{"语言":14} {"文件数":>8} {"总行数":>9} {"代码行":>9} {"注释行":>8} {"空行":>8}')
    print('-' * 78)
    tot = {'files': 0, 'lines': 0, 'code': 0, 'comment': 0, 'blank': 0, 'bytes': 0}
    for lang, s in sorted(stats.items(), key=lambda kv: -kv[1]['lines']):
        print(f'{lang:14} {s["files"]:>8} {s["lines"]:>9} {s["code"]:>9} '
              f'{s["comment"]:>8} {s["blank"]:>8}')
        for k in tot:
            tot[k] += s[k]
    print('-' * 78)
    print(f'{"合计":14} {tot["files"]:>8} {tot["lines"]:>9} {tot["code"]:>9} '
          f'{tot["comment"]:>8} {tot["blank"]:>8}')
    print()

    code_stats = {k: v for k, v in stats.items() if k in CODE_LANG}
    code_lines = sum(v['lines'] for v in code_stats.values())
    code_files = sum(v['files'] for v in code_stats.values())
    print('=' * 78)
    print('二、软件著作权登记口径的"源程序量"')
    print('=' * 78)
    print(f'  编程语言（按行数排序）：')
    for lang, s in sorted(code_stats.items(), key=lambda kv: -kv[1]['lines']):
        pct = s['lines'] / max(1, code_lines) * 100
        print(f'     {lang:14} {s["lines"]:>8} 行 ({pct:>5.1f}%)  {s["files"]:>4} 文件')
    print()
    print(f'  源程序文件数：{code_files}')
    print(f'  源程序总行数：{code_lines}')
    print(f'  有效代码行数：{sum(v["code"] for v in code_stats.values())}')
    print(f'  源程序总字节：{sum(v["bytes"] for v in code_stats.values()):,}')
    print(f'  折合页数（每页 50 行）：{code_lines / 50:.0f} 页')
    print()

    print('=' * 78)
    print('三、核心模块（用于源程序 前30页/后30页 的取材与排序说明）')
    print('=' * 78)
    core = sorted([f for f in per_file if f['lang'] == 'Python'],
                  key=lambda f: -f['lines'])[:20]
    print(f'{"行数":>6}  {"文件":60}')
    for f in core:
        print(f'{f["lines"]:>6}  {f["path"]}')
    print()

    out = {
        'by_language': {k: dict(v) for k, v in stats.items()},
        'code_languages': {k: dict(v) for k, v in code_stats.items()},
        'source_files': code_files,
        'source_lines': code_lines,
        'source_code_lines': sum(v['code'] for v in code_stats.values()),
        'source_bytes': sum(v['bytes'] for v in code_stats.values()),
        'pages_at_50_lines': round(code_lines / 50),
        'all_files_count': tot['files'],
        'all_lines': tot['lines'],
        'core_python_modules': core,
    }
    dst = ROOT / 'docs' / '软著登记' / 'source_stats.json'
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'→ {dst.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
