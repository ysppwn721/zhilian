$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$model = 'models\bge-reranker-v2-m3-onnx-int8'
if (-not (Test-Path -LiteralPath $model)) { throw "Model directory missing: $model" }
$artifactDir = 'artifacts'
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
$zip = Join-Path $artifactDir 'Zhilian-bge-reranker-v2-m3-onnx-int8.zip'
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
$stage = Join-Path $env:TEMP 'zhilian-model-bundle'
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'models') | Out-Null
Copy-Item -LiteralPath $model -Destination (Join-Path $stage 'models') -Recurse
Copy-Item -LiteralPath 'packaging\install_model_windows.bat' -Destination $stage
# Keep the same layout as the Linux tarball: extract beside the application
# and the launcher will discover models\bge-reranker-v2-m3-onnx-int8.
Compress-Archive -Path @((Join-Path $stage 'models'), (Join-Path $stage 'install_model_windows.bat')) -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -LiteralPath $stage -Recurse -Force
Write-Host "Created $zip"
