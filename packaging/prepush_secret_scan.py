"""上传前密钥与敏感信息扫描。

扫描范围：即将提交的内容（已跟踪文件 + 工作区未忽略文件），以及 git 历史。
只报告"文件:行号 + 匹配类型 + 脱敏片段"，**不输出完整密钥**。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent

# 密钥/令牌形态
PATTERNS = [
    ('DeepSeek/OpenAI 风格密钥', re.compile(r'sk-[A-Za-z0-9_\-]{16,}')),
    ('Bearer 令牌', re.compile(r'Bearer\s+[A-Za-z0-9_\-\.]{20,}')),
    ('赋值式 API Key', re.compile(r'(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*[\'"]?([A-Za-z0-9_\-]{16,})')),
    ('私钥块', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('AWS AK', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('GitHub token', re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}')),
]
# 允许的占位符/示例值
PLACEHOLDER = re.compile(r'(?i)^(your|xxx|placeholder|example|test|dummy|changeme|redacted|\*+|<.*>|\$\{.*\})')

SKIP_DIRS = {'.venv', '.git', 'node_modules', 'site-packages', '.train-cuda-venv',
             'dist', '__pycache__', '.build-venv'}
SKIP_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.docx', '.xlsx', '.zip',
            '.onnx', '.bin', '.safetensors', '.pyc', '.ico', '.woff', '.woff2', '.ttf'}


def mask(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[0] + '***'
    return f'{s[:4]}…{s[-2:]}(len {len(s)})'


def iter_files():
    """遍历工作区文件（跳过忽略目录与二进制）。"""
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        parts = set(p.parts)
        if parts & SKIP_DIRS:
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        if p.stat().st_size > 3_000_000:
            continue
        yield p


def scan_text(text: str, label: str, hits: list):
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS:
            for m in pat.finditer(line):
                val = m.group(m.lastindex) if m.lastindex else m.group(0)
                if PLACEHOLDER.match(str(val)):
                    continue
                # 排除对已知占位文件名/文档名的误报
                if 'human_gold_api.env' in line or 'llm.py' in line:
                    continue
                hits.append((label, lineno, name, mask(str(val)), line.strip()[:90]))
                break


def main() -> int:
    print('=== 1) 工作区扫描 ===')
    hits: list = []
    n = 0
    for p in iter_files():
        n += 1
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        scan_text(text, str(p.relative_to(ROOT)), hits)
    print(f'  扫描 {n} 个文件，命中 {len(hits)} 处')
    for label, lineno, name, masked, ctx in hits[:40]:
        print(f'    {label}:{lineno}  [{name}]  {masked}')
        print(f'        {ctx}')
    print()

    print('=== 2) .env 类文件是否会被提交 ===')
    for p in ROOT.rglob('.env*'):
        if any(d in p.parts for d in SKIP_DIRS):
            continue
        rel = p.relative_to(ROOT)
        tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', str(rel)],
                                 cwd=ROOT, capture_output=True, text=True)
        ignored = subprocess.run(['git', 'check-ignore', str(rel)],
                                 cwd=ROOT, capture_output=True, text=True)
        status = '已跟踪（会提交）' if tracked.returncode == 0 else (
            '被 .gitignore 忽略' if ignored.returncode == 0 else '未跟踪但**不会被忽略**')
        flag = '⚠' if status.startswith('已跟踪') or '不会被忽略' in status else '✓'
        print(f'  {flag} {rel}  → {status}')
    print()

    print('=== 3) git 历史扫描（提交信息 + 已提交内容）===')
    hist = subprocess.run(['git', 'log', '--all', '-p', '--no-color'],
                          cwd=ROOT, capture_output=True, text=True, errors='ignore')
    hhits = []
    scan_text(hist.stdout, 'history', hhits)
    print(f'  历史命中 {len(hhits)} 处')
    for label, lineno, name, masked, ctx in hhits[:20]:
        print(f'    [{name}]  {masked}   {ctx[:70]}')
    print()

    print('=== 4) 结论 ===')
    total = len(hits) + len(hhits)
    if total == 0:
        print('  ✓ 未发现密钥泄漏')
    else:
        print(f'  ⚠ 发现 {total} 处需人工确认（可能是占位符或测试值）')
    return 0 if total == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
