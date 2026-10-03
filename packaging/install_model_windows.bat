@echo off
setlocal
set "SRC=%~dp0models\bge-reranker-v2-m3-onnx-int8"
set "ROOT=%LOCALAPPDATA%\Zhilian"
set "DST=%ROOT%\models\bge-reranker-v2-m3-onnx-int8"
if not exist "%SRC%\onnx\model_int8.onnx" (
  echo 未找到模型目录：%SRC%
  echo 请将本脚本放在模型 ZIP 解压后的根目录运行。
  exit /b 1
)
if not exist "%ROOT%\models" mkdir "%ROOT%\models"
robocopy "%SRC%" "%DST%" /E /NFL /NDL /NJH /NJS >nul
if errorlevel 8 exit /b 1
if not exist "%ROOT%\.env" echo DEEPSEEK_API_KEY=>>"%ROOT%\.env"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=$env:LOCALAPPDATA + '\Zhilian\.env'; $x=if(Test-Path $p){Get-Content $p}else{@()}; $x=$x|?{$_ -notmatch '^ZHILIAN_LOCAL_RERANKER_ENABLED=' -and $_ -notmatch '^ZHILIAN_LOCAL_RERANKER_PATH='}; Add-Content $p 'ZHILIAN_LOCAL_RERANKER_ENABLED=1'; Add-Content $p ('ZHILIAN_LOCAL_RERANKER_PATH=' + $env:LOCALAPPDATA + '\Zhilian\models\bge-reranker-v2-m3-onnx-int8')"
echo 本地模型已安装到 %DST%
echo 重新启动知链即可启用。
