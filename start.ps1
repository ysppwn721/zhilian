$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    # 运行时依赖而非 requirements.txt：后者额外含 pandas/pdfplumber 与本地重排模型三项，
    # 只被 答辩评测/ 下的评测脚本使用（详见 requirements-runtime.txt 顶部说明）。
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-runtime.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
Write-Host 'Zhilian: http://127.0.0.1:8765  (Ctrl+C to stop)'
& '.\.venv\Scripts\python.exe' run.py
