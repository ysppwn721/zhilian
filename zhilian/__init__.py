"""知链：跨文档结论验证与增量修复。

版本号的唯一真源是仓库根目录的 `VERSION` 文件。此前版本字面量散落在 app.py 的
下载白名单、两份 PyInstaller spec、Inno Setup 脚本、CI 工作流、站点页面与启动
脚本共 6 处，漏改任一处就会出现"页面写着新版本、下载文件名还是旧版本"或下载
404（下载白名单是精确匹配）。运行 `python packaging/sync_version.py` 传播，
`--check` 供 CI 拦截漂移。
"""
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION'


def _read_version():
    try:
        # utf-8-sig：容忍编辑器在 VERSION 开头写入 BOM。
        text = _VERSION_FILE.read_text(encoding='utf-8-sig').strip()
    except OSError:
        return '0.0.0'
    return text or '0.0.0'


__version__ = _read_version()
