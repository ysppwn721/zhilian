#!/usr/bin/env python3
"""把根目录 VERSION 的版本号传播到所有需要它的位置。

为什么需要这个脚本
------------------
版本字面量原先散落在 6 处：app.py 的下载白名单（10 个文件名内嵌版本）、
两份 PyInstaller spec、Inno Setup 脚本、CI 工作流、站点页面、启动脚本。
漏改任一处不是"显示不一致"这么轻——`/downloads/{filename}` 是**精确匹配白名单**，
文件名带旧版本号就会直接 404。

用法
----
    python packaging/sync_version.py          # 就地传播
    python packaging/sync_version.py --check  # 只检查，有漂移则退出码 1（供 CI）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Windows 控制台默认是 GBK（cp936），脚本里的 ✓ / ✗ 会直接抛 UnicodeEncodeError。
# 强制标准流走 UTF-8，无法编码的字符退化为替代符而不是让整个脚本崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / 'VERSION'

# 需要同步的文件清单。任何一个不存在都说明仓库结构变了，应当显式报告而不是静默跳过。
MANAGED_FILES = (
    'zhilian/app.py',
    'packaging/zhilian.spec',
    'packaging/zhilian-linux.spec',
    'packaging/zhilian.iss',
    'packaging/build.sh',
    'packaging/build.ps1',
    'packaging/make_zip.py',
    '.github/workflows/build-installers.yml',
    'site/index.html',
    'README.md',
)

# 每项 = (相对路径, 正则, 替换模板)；正则里的 {v} 会先被换成"任意版本号"捕获组。
# 全部锚定到带版本号的具体形态，避免误伤无关数字。
RULES = [
    # 后端下载白名单：文件名内嵌版本，漏改即 404（精确匹配）。
    ('zhilian/app.py', r"v{v}_Windows安装版\.exe"),
    ('zhilian/app.py', r"v{v}_Windows免安装\.zip"),
    ('zhilian/app.py', r"v{v}_跨平台构建包\.zip"),
    ('zhilian/app.py', r"Zhilian-{v}-linux-x86_64\.tar\.gz"),
    ('zhilian/app.py', r"Zhilian-{v}-linux-x86_64-glibc228\.tar\.gz"),
    ('zhilian/app.py', r"Zhilian-{v}-linux-x86_64-src\.tar\.gz"),
    ('zhilian/app.py', r"v{v}_源代码\.zip"),
    # FastAPI 元数据 / 健康检查返回的版本。
    ('zhilian/app.py', r"version='{v}'"),
    ('zhilian/app.py', r"'version': '{v}'"),
    # PyInstaller 打包元数据。
    ('packaging/zhilian.spec', r"'CFBundleShortVersionString': '{v}'"),
    ('packaging/zhilian-linux.spec', r"'CFBundleShortVersionString': '{v}'"),
    # Inno Setup：三处。
    ('packaging/zhilian.iss', r"^AppVersion={v}$"),
    ('packaging/zhilian.iss', r"^OutputBaseFilename=Zhilian-{v}-windows-x64-setup$"),
    ('packaging/zhilian.iss', r"^UninstallDisplayName=知链 {v}$"),
    # CI 产物名。
    ('.github/workflows/build-installers.yml', r"name: Zhilian-{v}-\$\{\{ matrix\.archive \}\}"),
    # 站点页面：下载链接的文件名与版本徽标。
    ('site/index.html', r"可下载 · v{v}"),
    ('site/index.html', r"版本 v{v} ｜"),
    ('site/index.html', r"v{v}_Windows安装版\.exe"),
    ('site/index.html', r"v{v}_Windows免安装\.zip"),
    ('site/index.html', r"v{v}_跨平台构建包\.zip"),
    ('site/index.html', r"v{v}_源代码\.zip"),
    ('site/index.html', r"Zhilian-{v}-linux-x86_64"),
    # 启动脚本横幅已改为运行时读取 VERSION，无需再同步字面量。
    # 打包脚本里的 version 变量。
    ('packaging/build.sh', r'^version="{v}"$'),
    ('packaging/build.ps1', r"\$version\s*=\s*'{v}'"),
    ('packaging/make_zip.py', r"Zhilian-{v}-windows-x64\.zip"),
    # README 的"当前版本"。
    ('README.md', r"当前版本：{v}。"),
]

# 版本号占位符（{v} → 具名捕获组）。多重定义时只取一处匹配即可。
_VERSION_TOKEN = r'(?P<v>\d+\.\d+\.\d+)'

_IGNORE_LITERALS = {'1.0'}  # .desktop 的 Version=1.0 是桌面规范版本，不是应用版本


def compile_rules():
    out = []
    for rel, pattern in RULES:
        flags = re.MULTILINE if ('^' in pattern or '$' in pattern) else 0
        out.append((rel, re.compile(pattern.replace('{v}', _VERSION_TOKEN), flags)))
    return out


def read_version() -> str:
    # utf-8-sig：容忍编辑器在 VERSION 开头写入 BOM。
    return VERSION_FILE.read_text(encoding='utf-8-sig').strip()


def rewrite(text: str, pattern: re.Pattern, new_version: str) -> str:
    """把 pattern 命中的版本号统一替换为 new_version（只改数字部分）。"""
    def _sub(match: re.Match) -> str:
        return match.group(0).replace(match.group('v'), new_version)
    return pattern.sub(_sub, text)


def find_uncovered(new_version: str):
    """列出"不会被任何规则改写"的版本字面量。

    判定方式是把每条规则在原文本上的匹配区间标出来，再看哪些 x.y.z 落在所有
    区间之外。不能用"改完再扫"——那次扫描读到的是改写前的内容，会把已经处理过的
    位置误报成未覆盖。
    """
    uncovered = []
    for rel in sorted({r for r, _ in RULES}):
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        covered = []
        for rule_rel, pattern in compile_rules():
            if rule_rel != rel:
                continue
            covered.extend(m.span() for m in pattern.finditer(text))

        def inside(start: int) -> bool:
            return any(lo <= start < hi for lo, hi in covered)

        for hit in re.finditer(r'(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])', text):
            value = hit.group(1)
            if value == new_version or value in _IGNORE_LITERALS or inside(hit.start()):
                continue
            lineno = text[:hit.start()].count('\n') + 1
            uncovered.append(f'{rel}:{lineno}: {value}')
    return uncovered


def sync(new_version: str, check_only: bool):
    """返回 (changing, missing)。changing 为需要同步 / 已同步的文件（去重）。"""
    changing, missing = set(), set()
    for rel, pattern in compile_rules():
        path = ROOT / rel
        if not path.is_file():
            missing.add(rel)
            continue
        text = path.read_text(encoding='utf-8')
        updated = rewrite(text, pattern, new_version)
        if updated != text:
            changing.add(rel)
            if not check_only:
                path.write_text(updated, encoding='utf-8')
    return sorted(changing), sorted(missing)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='只检查，不写文件')
    args = parser.parse_args()

    new_version = read_version()
    if not re.fullmatch(r'\d+\.\d+\.\d+', new_version):
        print(f'✗ VERSION 内容不是 x.y.z：{new_version!r}', file=sys.stderr)
        return 2

    changing, missing = sync(new_version, check_only=args.check)
    uncovered = find_uncovered(new_version)

    print(f'VERSION = {new_version}')
    for rel in changing:
        print(f'  {"需要同步" if args.check else "已同步  "}  {rel}')
    for rel in missing:
        print(f'  ? 清单中的文件不存在  {rel}')
    for item in uncovered:
        print(f'  ? 未被任何规则覆盖的版本字面量  {item}（需人工确认）')

    if args.check:
        if changing:
            print('\n✗ 版本号存在漂移。执行 python packaging/sync_version.py 后再提交。',
                  file=sys.stderr)
            return 1
        print('\n✓ 清单内所有位置与 VERSION 一致')
        return 0

    print(f'\n✓ 已传播到 {len(changing)} 个文件' if changing else '\n✓ 无需改动')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
