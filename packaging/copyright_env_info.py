"""采集软著登记所需的开发/运行环境信息（本机实测，不估算）。"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent


def ps(cmd: str) -> str:
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60)
        return (r.stdout or '').strip()
    except Exception as exc:                       # noqa: BLE001
        return f'<失败: {type(exc).__name__}>'


info: dict = {}

print('=' * 76)
print('一、本机（开发环境）实测')
print('=' * 76)

info['os'] = {
    'system': platform.system(),
    'release': platform.release(),
    'version': platform.version(),
    'caption': ps('(Get-CimInstance Win32_OperatingSystem).Caption'),
    'build': ps('(Get-CimInstance Win32_OperatingSystem).BuildNumber'),
    'arch': platform.machine(),
}
print(f"  操作系统   : {info['os']['caption']}  ({info['os']['system']} {info['os']['release']})")
print(f"  系统版本   : {info['os']['version']}")
print(f"  系统架构   : {info['os']['arch']}")

info['cpu'] = {
    'name': ps('(Get-CimInstance Win32_Processor).Name'),
    'cores': ps('(Get-CimInstance Win32_Processor).NumberOfCores'),
    'threads': ps('(Get-CimInstance Win32_Processor).NumberOfLogicalProcessors'),
    'max_clock_mhz': ps('(Get-CimInstance Win32_Processor).MaxClockSpeed'),
}
print(f"  CPU        : {info['cpu']['name']}")
print(f"  核心/线程  : {info['cpu']['cores']} 核 / {info['cpu']['threads']} 线程")

ram = ps('[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)')
info['ram_gb'] = ram
print(f"  内存       : {ram} GB")

gpu = ps('(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name')
gpus = ps('(Get-CimInstance Win32_VideoController).Name') 
info['gpu'] = gpu
info['gpu_list'] = [g for g in gpus.splitlines() if g.strip()]
print(f"  显卡       : {gpu}")
if len(info['gpu_list']) > 1:
    print(f"  全部显卡   : {info['gpu_list']}")

disk = ps('[math]::Round((Get-CimInstance Win32_LogicalDisk -Filter "DeviceID=\'C:\'").Size/1GB,0)')
info['disk_c_gb'] = disk
print(f"  C 盘容量   : {disk} GB")

print()
print('=' * 76)
print('二、运行时软件栈')
print('=' * 76)
info['python'] = sys.version.split()[0]
print(f"  Python     : {info['python']}")
for mod in ('PySide6', 'PyMuPDF', 'pdfplumber', 'openpyxl', 'python-docx', 'httpx',
            'numpy', 'onnxruntime', 'torch', 'matplotlib', 'pytest'):
    try:
        m = __import__(mod)
        v = getattr(m, '__version__', '')
        print(f"  {mod:14}: {v}")
        info.setdefault('packages', {})[mod] = v
    except Exception:                              # noqa: BLE001
        pass

print()
print('=' * 76)
print('三、运行平台线索（从打包与发布脚本推断）')
print('=' * 76)
for f in ('requirements.txt', 'requirements-runtime.txt', 'start.ps1', 'start.sh',
          'install.sh', 'setup.sh'):
    p = ROOT / f
    if p.is_file():
        txt = p.read_text(encoding='utf-8', errors='replace')
        n = len(txt.splitlines())
        print(f'  {f:26} {n:>4} 行')
for d in ('deploy', '.github/workflows', 'site/downloads'):
    p = ROOT / d
    if p.is_dir():
        files = [x.name for x in p.iterdir() if x.is_file()][:6]
        print(f'  {d:26} {len(list(p.iterdir())):>4} 项  {files}')

# 发布包命名可反推目标平台
print()
print('  发布产物命名（反映目标平台）：')
for pat in ('Zhilian-*.tar.gz', 'Zhilian-*.zip', '知链_*.zip*', '*windows*', '*linux*',
            '*macos*', '*.exe', '*.msi'):
    for f in ROOT.rglob(pat):
        if any(x in f.parts for x in ('.git', '.venv', 'node_modules')):
            continue
        print(f'    {f.relative_to(ROOT)}')
        break

print()
print('=' * 76)
print('四、网络依赖（影响"运行环境"表述）')
print('=' * 76)
print('  本地模型：ONNX（BGE reranker）与 PyTorch（BERT），可离线运行')
print('  远程模型：DeepSeek API（可选，api_only/hybrid 模式），需 HTTPS 出网')
print('  默认模式：hybrid —— 无网络时自动降级为纯规则')

dst = ROOT / 'docs' / '软著登记' / 'runtime_env.json'
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
print()
print(f'→ {dst.relative_to(ROOT)}')
