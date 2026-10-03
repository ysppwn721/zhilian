# Linux 安装包设计说明

> 产物：`Zhilian-0.2.1-linux-x86_64-src.tar.gz`（源码安装包）和 `Zhilian-0.2.1-linux-x86_64.tar.gz`（原生免 Python 包）
> 设计目标：**不依赖 root、不污染系统环境、覆盖主流发行版、可一键卸载**
> 与 Windows/macOS 原生包的关系见第五节（两条路线并存，互不替代）

## 一、为什么是这个形态

Linux 上没有统一的安装包格式。可选方案与取舍：

| 方案 | 覆盖范围 | 需要 root | 构建要求 | 采用 |
| --- | --- | --- | --- | --- |
| **tar.gz + 安装脚本** | 全部发行版 | 否 | 任意平台可打包 | ✅ **主方案** |
| AppImage | 全部发行版 | 否 | 需 Linux + FUSE | 备选 |
| .deb | Debian/Ubuntu | 是 | 需 Linux + dpkg | 补充 |
| .rpm | RHEL/Fedora | 是 | 需 Linux + rpmbuild | 补充 |
| Docker 镜像 | 有 Docker 的机器 | 否 | 需 Linux | ✅ 已提供 |

**选 tar.gz 的核心理由**：一份产物覆盖 Ubuntu / Debian / Fedora / Arch / openSUSE，
且**在 Windows 上就能打包交付**（.deb/.rpm/AppImage 都必须在 Linux 上构建）。

## 二、安装布局（全部在用户目录内）

```
~/.local/share/biaoliruyi/            ← 程序目录
    ├── zhilian/  web/  tests/  third_party/
    ├── .venv/                        ← 首次启动自动创建
    └── .zhilian/                     ← 项目数据（卸载时默认保留）

~/.local/bin/biaoliruyi               ← 启动命令
~/.local/share/applications/biaoliruyi.desktop   ← 应用菜单项
~/.local/share/icons/hicolor/256x256/apps/zhilian.png      ← 知链图标
```

**为什么不用 `/opt` 或 `/usr/local`**：那些位置需要 root。用户级安装换来的是
**无需 sudo、不污染系统、卸载即净**，且符合 XDG 规范，主流桌面环境都能识别。

## 三、三个脚本的职责

| 脚本 | 作用 | 关键设计 |
| --- | --- | --- |
| `setup.sh` | **统一入口** | 先补齐执行权限，再交给 install.sh |
| `install.sh` | 布置程序、建命令、装桌面项 | 生成图标、刷新桌面数据库 |
| `uninstall.sh` | 卸载 | **默认保留项目数据**，`--purge` 才删 |

### 为什么需要 `setup.sh`

**这是从 Windows 打包时必然遇到的问题**：tar/zip 在 NTFS 上不携带 Unix 权限位，
打出的包解压后 `*.sh` 是 `0666`，用户执行 `./install.sh` 会得到
`Permission denied`。

实测确认：

```
$ tar -tzvf Zhilian-0.2.1-linux-x86_64-src.tar.gz | grep install.sh
-rw-rw-rw-  0 0  5275 ... install.sh      ← 无执行位
```

因此文档要求用 **`bash setup.sh`** 而非 `./setup.sh` —— 前者不依赖执行权限。
`setup.sh` 会先 `chmod +x` 再继续，用户不会看到权限报错。

### 卸载为什么默认保留数据

`.zhilian/` 里是用户导入的项目、版本快照与审计记录。误删的代价远大于残留的代价，
所以默认保留并明确提示，删除需显式加 `--purge`。

## 四、已处理的 Linux 特有问题

| 问题 | 处理 |
| --- | --- |
| **PEP 668**：现代发行版禁止直接 `pip install` 到系统 Python | 一律使用项目内 `.venv`，不动系统环境 |
| **缺少 `python3-venv`**：Debian/Ubuntu 默认不装 | 捕获失败并给出对应发行版的安装命令 |
| **端口被占用**：残留进程导致「启动了却打不开」 | 启动前用 `ss`/`lsof` 检测并给出 `pkill` 与换端口两种解法 |
| **CRLF 行尾**：Windows 上 git 转换后 shebang 失效 | 新增 `.gitattributes` 锁定 `*.sh` 为 LF |
| **无桌面环境**（服务器） | 自动检测 `DISPLAY`/`WAYLAND_DISPLAY`，无则跳过打开浏览器 |
| **图标缺失工具**（ImageMagick） | 用纯 Python 内联生成 PNG，不引入外部依赖 |
| **`update-desktop-database` 不存在** | 静默跳过，不影响安装 |

## 五、与 PyInstaller 路线的关系

项目内同时存在两条 Linux 交付路线，**面向不同场景，互不替代**：

| | 本方案（src tar.gz） | PyInstaller（`packaging/build.sh`） |
| --- | --- | --- |
| 产物 | `Zhilian-0.2.1-linux-x86_64-src.tar.gz` | `Zhilian-0.2.1-linux-x86_64.tar.gz` |
| 体积 | **0.38 MB** | 约 28 MB+ |
| 需要 Python | 3.10+ | **不需要** |
| 构建环境 | 任意平台 | **必须 Linux** |
| 桌面菜单集成 | **有** | 无 |
| 当前可交付 | **是** | **已在 WSL Ubuntu 22.04 构建并完成健康检查** |

**建议**：两者都发布，在下载页标注清楚需要 Python 与否。
多数 Linux 用户本机已有 Python，src 包更轻；无 Python 或需免安装的场景用原生包。

## 国产系统兼容基础包（2026-09-27 新增）

下载页已增加统信 UOS / 银河麒麟 x86_64 兼容包，详情见 [DOMESTIC_LINUX.md](DOMESTIC_LINUX.md)。
已通过 glibc 2.28 容器启动及 Ubuntu WSL 离线业务闭环检查；目标系统实机验收仍待完成。
此基础包不含本地语义模型及推理依赖，不适用于 ARM64、龙芯、申威。

## 六、验收命令

```bash
# 1. 解压并安装
tar -xzf Zhilian-0.2.1-linux-x86_64-src.tar.gz
cd Zhilian-0.2.1-linux-x86_64-src
bash setup.sh

# 2. 启动（首次会建虚拟环境，约 1—3 分钟）
biaoliruyi
# 或： ~/.local/share/biaoliruyi/start.sh

# 3. 验证
curl -s http://127.0.0.1:8765/api/health
# 若设了密码则返回 401；带认证：
curl -s -u zhilian:你的密码 http://127.0.0.1:8765/api/health

# 4. 桌面菜单
#    搜索「表里如一」，应能直接启动

# 5. 卸载（保留数据）
bash uninstall.sh
#    连数据一起删： bash uninstall.sh --purge
```

## 七、未在本机验证的部分（如实说明）

本机为 Windows，通过 WSL Ubuntu 22.04 完成了 Linux 原生包构建和安装启动验证。已完成的验证：

- ✅ 三个脚本 `bash -n` 语法检查全部通过（用 Git 自带 bash）
- ✅ 行尾为纯 LF（无 CRLF 污染）
- ✅ 图标生成代码实测产出合法 PNG（256×256，Pillow 校验通过）
- ✅ 安装脚本引用的文件均在包内
- ✅ 站点下载链接与锚点校验通过
- ✅ PyInstaller Linux x86_64 原生包构建成功
- ✅ 原生包 `bash install.sh` 用户级安装成功
- ✅ 安装后的 `~/.local/bin/zhilian` 启动并返回 `/api/health` 200

**仍需在真实桌面 Linux 机器上补做的验证**：

- ⚠️ 桌面菜单项在各桌面环境（GNOME/KDE/XFCE）的实际显示
- ⚠️ ONNX Runtime 在 Linux 上的动态库加载（若启用本地 reranker）

**首次在 Linux 上验收时，请优先确认这三项。**
