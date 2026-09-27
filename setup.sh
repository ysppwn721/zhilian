#!/usr/bin/env bash
# 表里如一 —— 安装入口（推荐第一个运行的脚本）
#
# 存在的理由：从 Windows 打包的 tar.gz 不携带 Unix 权限位，
# 解压后 *.sh 可能是 0666，直接 ./install.sh 会报 Permission denied。
# 用 `bash setup.sh` 调用则不依赖执行权限，因此把本脚本作为统一入口。
set -eu

cd "$(dirname "$(readlink -f "$0")")" || exit 1
ROOT="$(pwd)"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'

printf '\n%s表里如一 · 安装准备%s\n\n' "$BOLD" "$RESET"

# 1. 补齐执行权限（对解压自 zip/tar 的场景必需）
fixed=0
for f in start.sh install.sh uninstall.sh; do
    if [ -f "$f" ] && [ ! -x "$f" ]; then
        chmod +x "$f" 2>/dev/null && fixed=$((fixed + 1)) || true
    fi
done
if [ "$fixed" -gt 0 ]; then
    printf '%s✓%s 已为 %d 个脚本补齐执行权限\n' "$GREEN" "$RESET" "$fixed"
else
    printf '%s✓%s 脚本权限正常\n' "$GREEN" "$RESET"
fi

# 2. 校验必需文件
missing=""
for f in run.py requirements-runtime.txt start.sh install.sh zhilian web VERSION; do
    [ -e "$ROOT/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
    printf '%s✗ 安装包不完整，缺少：%s%s\n' "$RED" "$missing" "$RESET" >&2
    printf '  请在完整解压后的目录内运行本脚本。\n' >&2
    exit 1
fi
printf '%s✓%s 安装包完整\n' "$GREEN" "$RESET"

# 3. 交给主安装脚本
printf '\n%s\n\n' "开始安装……"
exec bash "$ROOT/install.sh" "$@"
