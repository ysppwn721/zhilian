#!/usr/bin/env bash
# 表里如一 · 跨文档结论一致性治理系统 —— Linux 启动脚本
# 首次运行自动创建虚拟环境并安装依赖；之后直接启动。
set -u

# 切到脚本所在目录，保证相对路径可用（等价于 Windows 版 start.ps1 的 Set-Location）
cd "$(dirname "$(readlink -f "$0")")" || exit 1
ROOT="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

printf '\n%s表里如一 · 跨文档结论一致性治理系统%s\n' "$BOLD" "$RESET"
# 版本横幅取自根目录 VERSION（唯一真源），不再硬编码——否则每发一版都要记得改这里。
APP_VERSION="$(tr -d '\r\n' < "$ROOT/VERSION" 2>/dev/null || true)"
printf '%sLinux 免安装版  v%s%s\n\n' "$DIM" "${APP_VERSION:-未知}" "$RESET"

# ---- 1. 选择 Python 解释器（3.10+；优先 3.12/3.11）----------------------
PY=""
for cand in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    die "未找到 Python 3.10 或更高版本。
  请先安装，例如：
    Ubuntu/Debian : sudo apt install python3 python3-venv python3-pip
    Fedora/RHEL   : sudo dnf install python3 python3-pip
    Arch          : sudo pacman -S python python-pip"
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python $PYVER（$PY）"

# ---- 2. 首次运行：创建虚拟环境并安装依赖 ---------------------------------
# 说明：现代发行版对系统 Python 启用了 PEP 668（externally-managed-environment），
# 直接 pip install 会被拒绝。因此一律使用项目内的 .venv，不动系统环境。
VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    printf '\n%s[1/3]%s 首次运行：创建虚拟环境 .venv\n' "$BOLD" "$RESET"
    if ! "$PY" -m venv .venv 2>/dev/null; then
        rm -rf .venv
        die "创建虚拟环境失败。多数情况是缺少 venv 模块：
    Ubuntu/Debian : sudo apt install python3-venv
    Fedora/RHEL   : sudo dnf install python3-libs"
    fi
    printf '%s[2/3]%s 安装依赖（需联网，约 1—3 分钟）\n' "$BOLD" "$RESET"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    # 装运行时依赖而非 requirements.txt：后者含 pandas/pdfplumber 与本地重排模型三项，
    # 只被 答辩评测/ 下的评测脚本使用，装进产品环境纯属浪费（100MB+）。
    if ! "$VENV_PY" -m pip install -r requirements-runtime.txt; then
        die "依赖安装失败。请检查网络后重试。
  若使用国内网络，可加镜像：
    $VENV_PY -m pip install -r requirements-runtime.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    fi
    ok "依赖安装完成"
else
    ok "已存在虚拟环境，跳过安装"
fi

# ---- 3. 端口占用检查（避免出现「启动了却访问不到」）----------------------
PORT="${ZHILIAN_PORT:-8765}"
HOST="${ZHILIAN_HOST:-127.0.0.1}"
port_busy() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
    else
        return 1
    fi
}
if port_busy; then
    warn "端口 $PORT 已被占用。"
    say  "  可能是上一次未退出的实例。处理方式："
    say  "    pkill -f 'run.py'          # 结束残留进程后重试"
    say  "    ZHILIAN_PORT=8766 ./start.sh  # 或换端口启动"
    exit 1
fi

# ---- 4. 启动 ------------------------------------------------------------
printf '\n%s[3/3]%s 启动服务\n' "$BOLD" "$RESET"
printf '  访问地址 : %shttp://%s:%s%s\n' "$BOLD" "$HOST" "$PORT" "$RESET"
if [ -n "${ZHILIAN_ACCESS_PASSWORD:-}" ]; then
    printf '  登录账号 : %s（用户名 zhilian）\n' "$ZHILIAN_ACCESS_PASSWORD"
else
    printf '  登录账号 : %s未设置密码%s（仅限本机访问）\n' "$DIM" "$RESET"
fi
printf '  停止服务 : Ctrl+C\n\n'

# 若可用则自动打开浏览器
if command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    ( sleep 2; xdg-open "http://$HOST:$PORT" >/dev/null 2>&1 ) &
fi

exec "$VENV_PY" run.py
