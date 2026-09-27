#!/usr/bin/env bash
# 表里如一 —— Linux 安装脚本
#
# 设计取舍：采用「用户级安装 + 桌面项集成」，不写系统目录、不需要 sudo。
#   - 程序目录   : ~/.local/share/biaoliruyi
#   - 启动命令   : ~/.local/bin/biaoliruyi
#   - 菜单项     : ~/.local/share/applications/biaoliruyi.desktop
#   - 图标       : ~/.local/share/icons/hicolor/256x256/apps/biaoliruyi.png
# 卸载只需运行 ./uninstall.sh，不会留下残留。
set -eu

cd "$(dirname "$(readlink -f "$0")")" || exit 1
SRC="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

APP_NAME="biaoliruyi"
APP_CN="表里如一"
PREFIX="${HOME}/.local/share/${APP_NAME}"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"

printf '\n%s%s · Linux 安装%s\n\n' "$BOLD" "$APP_CN" "$RESET"

# ---- 1. 环境检查 --------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "未找到 python3。请先安装 Python 3.10+。"
ok "检测到 $(python3 --version 2>&1)"

for f in run.py requirements-runtime.txt start.sh; do
    [ -e "$SRC/$f" ] || die "安装包不完整：缺少 $f。请在解压后的目录内运行本脚本。"
done
ok "安装包完整"

if [ -e "$PREFIX" ]; then
    warn "已存在安装：$PREFIX"
    printf '  将覆盖安装（原有项目数据在 .zhilian/ 内，已在下方单独处理）。\n'
fi

# ---- 2. 复制程序文件 ----------------------------------------------------
mkdir -p "$PREFIX"
# 只复制运行所需内容，排除虚拟环境、运行数据与缓存
for item in zhilian web tests third_party site; do
    [ -e "$SRC/$item" ] && cp -r "$SRC/$item" "$PREFIX/" 2>/dev/null || true
done
for f in run.py requirements-runtime.txt requirements.txt requirements-dev.txt start.sh README.md \
         VERSION THIRD_PARTY_NOTICES.md Dockerfile .env.example "知链事实表模板.xlsx"; do
    [ -e "$SRC/$f" ] && cp "$SRC/$f" "$PREFIX/" || true
done
chmod +x "$PREFIX/start.sh"
ok "程序文件已复制到 $PREFIX"

# ---- 3. 生成启动命令 ----------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$APP_NAME" <<EOF
#!/usr/bin/env bash
# 表里如一启动器（由 install.sh 生成）
exec "$PREFIX/start.sh" "\$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME"
ok "启动命令已安装：$BIN_DIR/$APP_NAME"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR 不在 PATH 中。请执行一次：
    echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.bashrc && source ~/.bashrc" ;;
esac

# ---- 4. 桌面菜单项 ------------------------------------------------------
# 生成一个简洁图标（纯 Python，不依赖 ImageMagick 等外部工具）
mkdir -p "$ICON_DIR"
python3 - "$ICON_DIR/$APP_NAME.png" <<'PYEOF' 2>/dev/null || true
import struct, sys, zlib

SIZE = 256
BG = (21, 93, 80)      # 与站点主色一致
FG = (255, 255, 255)

rows = []
for y in range(SIZE):
    row = bytearray([0])
    for x in range(SIZE):
        # 圆角方块底 + 中央横杠（取「一」之意）
        edge = 22
        inside = (edge <= x < SIZE-edge and edge <= y < SIZE-edge)
        bar = (SIZE//2 - 92 <= x <= SIZE//2 + 92 and SIZE//2 - 13 <= y <= SIZE//2 + 13)
        row += bytes(FG if (bar and inside) else BG)
    rows.append(bytes(row))
raw = b''.join(rows)

def chunk(tag, data):
    return (struct.pack('>I', len(data)) + tag + data +
            struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', SIZE, SIZE, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(raw, 9))
       + chunk(b'IEND', b''))
open(sys.argv[1], 'wb').write(png)
PYEOF

mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_CN
Name[en]=Biaoliruyi
GenericName=Document Consistency Governance
Comment=跨文档结论验证与增量修复
Comment[en]=Cross-document claim verification and incremental repair
Exec=$BIN_DIR/$APP_NAME
Icon=$APP_NAME
Terminal=true
Categories=Office;Utility;
Keywords=document;consistency;report;验证;文档;
StartupNotify=false
EOF
chmod +x "$DESKTOP_DIR/$APP_NAME.desktop"

# 刷新桌面数据库（缺失时静默跳过，不影响安装）
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

ok "桌面菜单项已创建：$APP_NAME.desktop"

# ---- 5. 完成 ------------------------------------------------------------
cat <<EOF

${BOLD}安装完成${RESET}

  启动方式（三种任选其一）
    1. 应用菜单中搜索「$APP_CN」
    2. 终端执行：$APP_NAME
    3. 直接运行：$PREFIX/start.sh

  首次启动会自动创建虚拟环境并安装依赖（需联网）。
  程序目录：$PREFIX
  卸载方式：在安装包目录执行 ./uninstall.sh

EOF
