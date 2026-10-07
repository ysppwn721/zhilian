# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.building.build_main import Analysis, BUNDLE, COLLECT, EXE, PYZ

ROOT = Path(SPECPATH).resolve().parent
datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / ".env.example"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "multipart", "docx", "pptx", "openpyxl", "pydantic", "fastapi",
    "webview", "webview.platforms", "webview.platforms.edgechromium",
    "webview.platforms.winforms", "webview.platforms.mshtml",
    "bottle", "proxy_tools", "clr_loader", "pythonnet",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The base installer must stay small.  Local reranking is an optional
    # add-on; without these packages reranker.status() safely falls back to
    # deterministic rules/API mode.
    excludes=["torch", "transformers", "tensorflow", "pytest", "pandas", "matplotlib", "IPython", "mcp", "onnxruntime", "numpy", "tokenizers",
              "lxml.objectify", "lxml.html", "lxml.isoschematron", "PIL.ImageCms", "PIL.WebPImagePlugin"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Zhilian",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "web" / "assets" / "brand" / "concept-a.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Zhilian")
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='Zhilian.app', bundle_identifier='space.zhilian.desktop',
                 info_plist={'CFBundleShortVersionString': '0.2.1', 'NSHighResolutionCapable': True})
