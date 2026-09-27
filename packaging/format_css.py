"""把压缩成一行的 CSS 重新排版，只改空白，不改任何 token。

本项目 web/style.css 曾被压成 15 行（第 3 行 13985 字符），任何改动都会让 git diff
退化为"整行重写"。本脚本按 CSS 语法在 `{}` 与 `;` 处断行，并逐条声明换行，
不解析也不重写任何属性值——因此输出与输入在语义上逐字符等价。

安全守则：断行位置只在括号深度 0 且不在字符串字面量内时生效，
避免破坏 `font:14px/1.6 "Segoe UI",...` 这类含逗号与引号的值。

用法：
    python packaging/format_css.py web/style.css [--check]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass


def format_css(text: str, indent: str = '  ') -> str:
    out: list[str] = []
    line: list[str] = []
    depth = 0
    quote = ''
    parens = 0

    def flush(indent_level: int = 0) -> None:
        piece = ''.join(line).strip()
        line.clear()
        if piece:
            out.append(indent * indent_level + piece)

    for ch in text:
        # 字符串字面量内部原样透传（content:";" 这类不能当分隔符）。
        if quote:
            line.append(ch)
            if ch == quote:
                quote = ''
            continue
        if ch in '"\'':
            quote = ch
            line.append(ch)
            continue

        if ch == '(':
            parens += 1
        elif ch == ')':
            parens = max(0, parens - 1)

        if ch == '{' and parens == 0:
            line.append('{')
            flush(depth)
            depth += 1
        elif ch == '}' and parens == 0:
            flush(depth)
            depth = max(0, depth - 1)
            out.append(indent * depth + '}')
        elif ch == ';' and parens == 0:
            line.append(';')
            flush(depth)
        elif ch == '\n':
            # 已有的换行只在选定器位置保留；声明之间由上面的规则处理。
            if depth == 0:
                flush(0)
            else:
                line.append(' ')
        else:
            line.append(ch)

    flush(0)
    return '\n'.join(out) + '\n'


def strip_equivalent(a: str, b: str) -> bool:
    """忽略空白后比较，确认只是排版差异。"""
    return ''.join(a.split()) == ''.join(b.split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path', help='CSS 文件路径')
    parser.add_argument('--check', action='store_true', help='只检查是否需要排版')
    args = parser.parse_args()

    path = Path(args.path)
    original = path.read_text(encoding='utf-8')
    formatted = format_css(original)

    if not strip_equivalent(original, formatted):
        print(f'✗ 拒绝写入 {path}：排版前后非空白内容不一致（说明脚本有 bug）',
              file=sys.stderr)
        return 2

    if original == formatted:
        print(f'✓ {path} 已是排版后的形式')
        return 0

    if args.check:
        print(f'! {path} 未排版（{len(original.splitlines())} 行 → '
              f'{len(formatted.splitlines())} 行）', file=sys.stderr)
        return 1

    path.write_text(formatted, encoding='utf-8')
    print(f'✓ {path}：{len(original.splitlines())} 行 → {len(formatted.splitlines())} 行，'
          f'仅空白变化')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
