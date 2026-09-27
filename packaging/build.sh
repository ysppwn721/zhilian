#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(dirname "$(realpath "$0")")")"

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m PyInstaller --clean --noconfirm packaging/zhilian.spec
# Pillow's optional AVIF codec is not used by the document workflow.
find dist/Zhilian -type f -iname '*avif*' -delete 2>/dev/null || true
find dist/Zhilian -type f -iname '*imagingft*' -delete 2>/dev/null || true
cp packaging/start_portable.bat dist/Zhilian/start_portable.bat
cp packaging/install_native_linux.sh dist/Zhilian/install.sh
cp packaging/uninstall_native_linux.sh dist/Zhilian/uninstall.sh
cat > dist/Zhilian/README-LINUX.txt <<'EOF'
知链 Linux 原生包

安装（不需要 sudo）：
  bash install.sh

启动：
  ~/.local/bin/zhilian

卸载（保留项目数据）：
  bash uninstall.sh

项目数据默认位于 ~/.local/share/zhilian。

可选本地模型
基础包不包含约 570MB 的 BGE ONNX 模型。下载模型 tar.gz 后，在本目录执行：
  mkdir -p models
  tar -xzf Zhilian-bge-reranker-v2-m3-onnx-int8.tar.gz
  cp -a models/bge-reranker-v2-m3-onnx-int8 Zhilian/models/
  bash Zhilian/install.sh
安装脚本会自动启用本地 reranker；也可以不安装模型，继续使用规则/API 模式。
EOF
version="$(tr -d '\r\n' < VERSION)"
mkdir -p artifacts
platform="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
archive="artifacts/Zhilian-${version}-${platform}.tar.gz"
rm -f "$archive"
tar -czf "$archive" -C dist Zhilian
printf 'Created %s\n' "$archive"
