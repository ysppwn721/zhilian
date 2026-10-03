# 知链跨平台打包

打开项目后可在“处理模式”下拉框选择 `rules`、`local`、`hybrid`、`api` 或 `api_only`，按项目保存并立即生效于下次运行。默认值为 `hybrid`；桌面版可在用户配置目录 `.env` 设置 `ZHILIAN_LLM_MODE=hybrid` 并重启以更改默认值（Windows 为 `%LOCALAPPDATA%\Zhilian\.env`，Linux 为 `$XDG_DATA_HOME/zhilian/.env` 或 `~/.local/share/zhilian/.env`，macOS 为 `~/Library/Application Support/Zhilian/.env`）。`rules`/`local` 禁止远程请求；本地模型仍需单独安装。在线模式使用配置的 API Key 和额度，切换本身不产生调用。

基础包不内置本地 reranker，当前 Windows 便携包约 26MB，启动后即可运行规则、智能体、Word/PPT/Excel 处理和可选 API。双击 `Zhilian.exe` 或 `启动知链.bat` 会打开带知链图标的独立桌面窗口；没有 WebView2/GTK/Cocoa 时自动退回系统浏览器。同时提供 Inno Setup 标准安装版，支持选择安装目录、开始菜单、可选桌面快捷方式和卸载；用户项目数据保存在 `%LOCALAPPDATA%\Zhilian`，卸载程序不会删除这些数据。`models/bge-reranker-v2-m3-onnx-int8` 是独立的可选模型包。便携版解压到程序根目录下形成 `models\bge-reranker-v2-m3-onnx-int8`，新版启动器会自动发现并启用；标准安装版可放到 `%LOCALAPPDATA%\Zhilian\models\bge-reranker-v2-m3-onnx-int8`，并在同目录 `.env` 写入 `ZHILIAN_LOCAL_RERANKER_ENABLED=1`。

## Windows

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.16.0
.\packaging\build.ps1
```

产物：`artifacts/Zhilian-0.2.1-windows-x64.zip`。解压后运行 `Zhilian/Zhilian.exe`。数据默认写入 `%LOCALAPPDATA%\Zhilian\data`，不会写入安装目录。

标准安装版（需要 Inno Setup 6）：

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" packaging\zhilian.iss
```

产物：`artifacts/Zhilian-0.2.1-windows-x64-setup.exe`。默认安装到当前用户的 `%LOCALAPPDATA%\Programs\Zhilian`，可在向导中修改位置；安装完成后可从开始菜单或桌面快捷方式启动。

## macOS / Linux

在目标系统执行：

```bash
python3 -m pip install -r packaging/requirements.txt
bash packaging/build.sh
```

产物是 `artifacts/Zhilian-0.2.1-darwin-arm64.tar.gz`、`darwin-x86_64.tar.gz` 或 `linux-x86_64.tar.gz`，由目标机器架构决定。macOS 数据写入 `~/Library/Application Support/Zhilian/data`，Linux 数据写入 `$XDG_DATA_HOME/zhilian` 或 `~/.local/share/zhilian`。

当前已在 WSL Ubuntu 22.04 构建并验收 Linux x86_64 原生包：`artifacts/Zhilian-0.2.1-linux-x86_64.tar.gz`。解压后运行 `bash Zhilian/install.sh`，程序会安装到用户目录并生成 `~/.local/bin/zhilian` 和桌面入口；不需要 sudo，也不需要 Python。

## 可选模型包

Windows 执行 `packaging/build_model_bundle.ps1`；macOS/Linux 执行 `bash packaging/build_model_bundle.sh`。模型包与基础包分离，避免离线模型占用基础安装包体积。模型包约 409MB（压缩后），当前线上默认仍建议 BGE ONNX + 规则收紧候选。Windows 使用 ZIP 解压到 `Zhilian` 目录旁的 `models\`；Linux 使用 tar.gz 解压后放到 `Zhilian/models/`，再次运行安装脚本即可自动启用。

## 安装验收

设置 `ZHILIAN_NO_BROWSER=1` 后启动可用于无界面验收；默认启动会打开 `http://127.0.0.1:8765`。首次验收建议点击“体验演示项目”，完成一次 Excel → Word/PPT 的验证与导出。不要把 `.env` 或任何 API Key 放入压缩包。
