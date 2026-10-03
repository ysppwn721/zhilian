# 国产系统 x86_64 兼容基础包

更新：2026-09-27。适用产物：`Zhilian-0.2.1-linux-x86_64-glibc228.tar.gz`。

## 定位与边界

面向统信 UOS / 银河麒麟的 x86_64（amd64）环境，要求 glibc 2.28 或以上。
该包已在统信 UOS 与银河麒麟 x86_64 环境完成安装、启动、导入、修复与导出测试；不代表覆盖所有硬件架构或取得兼容认证。
不适用于 ARM64、龙芯、申威；操作系统名称相同并不意味着 CPU 架构相同。

内置 Python，无需另装 Python 或 sudo。启动后通过系统浏览器连接本机工作台。
纯规则模式可离线运行；在线模式需自行配置 API Key。包内不含密钥。
本包不含 BGE 模型以及 ONNX Runtime、numpy、tokenizers 推理依赖，单独复制模型权重不能启用本地语义功能。

## 下载与安装

从 `https://zhilian.space/#domestic-download` 选择「国产系统」卡片。
包体积为 20,459,921 字节，约 20.46 MB（19.51 MiB）。

先检查架构和运行库：

```bash
uname -m
ldd --version
```

应分别为 `x86_64` 和 glibc 2.28 或以上。在下载目录执行：

```bash
tar -xzf Zhilian-0.2.1-linux-x86_64-glibc228.tar.gz
cd Zhilian
bash install.sh
~/.local/bin/zhilian
```

默认应用目录：`~/.local/share/zhilian-app/Zhilian/`。
启动命令和应用菜单「知链」由安装脚本生成；安装前没有 `~/.local/bin/zhilian` 是正常情况。
配置：`~/.local/share/zhilian/.env`（权限 `0600`）。项目数据与安装目录分离。
自定义 XDG 目录时，以安装结束后的提示为准。

关闭启动终端或按 Ctrl+C 可停止服务。在解压目录执行 `bash uninstall.sh` 可卸载，保留项目数据。

## 已验证的范围

| 检查 | 结果 |
| --- | --- |
| 包成员路径、危险链接与 ELF 依赖 | 通过，无 glibc 高于 2.28 的已扫描 ELF 需求 |
| 干净 Debian 10 / glibc 2.28 容器启动 | `/api/health` 返回 200 |
| Ubuntu 22.04 WSL 用户级安装、启动 | 通过 |
| 创建演示、修改事实、确认来源、批准修复 | 通过，11 条修复后全部一致 |
| 导出 Excel / Word / PPT 与核验记录 | 通过，Word 原有背景段保留，PPT 可读取 |
| 撤销、卸载保留数据 | 通过 |
| 图标文件、桌面入口、配置权限 | 文件生成检查通过；`.env` 为 0600 |
| UOS / 麒麟 x86_64 安装、启动、导入、修复、导出 | 已完成 |
| 本地语义模型 | 此基础包不包含，不宣称已验证 |

审计脚本和原始结果位于 `.codex_artifacts/native_audit_20260927/`。
完整项目自动化测试本次为 260 passed（有一条依赖弃用警告）。

## 本次修复

1. 从同一份当前工作区代码重建，消除 `semantic_demo` 参数与旧版演示函数不一致。
2. 补齐冻结包 `docx/parts` 中间目录，解决 Word 默认页眉模板路径查找失败。
3. 安装脚本优先从 `_internal/web/` 复制图标，修正 PyInstaller 目录布局差异。
4. 配置创建及安装后使用 0600 权限。
5. 老 glibc 容器探测改用 bash TCP，不再依赖未安装的 curl。

SHA-256：

```text
25ca95bd55a9d04500a2fb27cd022a46e5186589ae29ca152d43ae541c82d6e9
```
