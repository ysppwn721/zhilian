"""检查生成 PDF 所需的中文字体与库。"""
import pathlib
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

print('=== Python PDF 库 ===')
for m in ('fitz', 'reportlab', 'fpdf', 'weasyprint', 'docx', 'openpyxl'):
    try:
        mod = __import__(m)
        print(f'  OK  {m:12} {getattr(mod, "__version__", "")}')
    except ImportError:
        print(f'  --  {m:12} 未安装')

print()
print('=== 等宽中文字体（源程序 PDF 需要）===')
cands = [
    r'C:\Windows\Fonts\simsun.ttc',      # 宋体
    r'C:\Windows\Fonts\simhei.ttf',      # 黑体
    r'C:\Windows\Fonts\msyh.ttc',        # 微软雅黑
    r'C:\Windows\Fonts\msyhmono.ttf',    # 等宽雅黑
    r'C:\Windows\Fonts\simfang.ttf',     # 仿宋
    r'C:\Windows\Fonts\simkai.ttf',      # 楷体
    r'C:\Windows\Fonts\Deng.ttf',        # 等线
    r'C:\Windows\Fonts\consola.ttf',     # Consolas
    r'C:\Windows\Fonts\cour.ttf',        # Courier New
]
for c in cands:
    p = pathlib.Path(c)
    print(f'  {"OK " if p.is_file() else "-- "} {p.name:16} {p}')

print()
print('=== 模拟：A4 每页能容纳的行数 ===')
# A4 = 595 x 842 pt。正文可用高度按上下各留 60pt 页边距估算。
page_h = 842
top_margin, bottom_margin = 60, 60
usable = page_h - top_margin - bottom_margin
for fname, fsize in (('小五 9pt', 9), ('五号 10.5pt', 10.5), ('小四 12pt', 12), ('四号 14pt', 14)):
    line_h = fsize * 1.25          # 单倍行距的常见近似
    print(f'  {fname:12} 行高 {line_h:>5.2f}pt → 每页约 {int(usable/line_h)} 行')
print()
print('  源程序要求每页 ≥50 行 → 9pt（行高 11.25pt，约 64 行/页）满足')
print('  文档要求每页 ≥30 行 → 12pt（行高 15pt，约 48 行/页）满足')
