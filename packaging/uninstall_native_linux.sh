#!/usr/bin/env bash
# Remove the native Linux application while preserving user project data.
set -eu

APP_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$APP_ROOT/zhilian-app"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

rm -rf "$APP_DIR"
rm -f "$BIN_DIR/zhilian" "$DESKTOP_DIR/zhilian.desktop"
rm -f "$ICON_DIR/zhilian.png"
echo "知链 Linux 程序已卸载，项目数据未删除。"
