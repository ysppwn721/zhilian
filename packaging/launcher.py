"""Desktop entry: writable configuration, persistent exports, safe local server."""
from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser


def user_root() -> Path:
    home = Path.home()
    if sys.platform == 'win32':
        return Path(os.getenv('LOCALAPPDATA', str(home / 'AppData/Local'))) / 'Zhilian'
    if sys.platform == 'darwin':
        return home / 'Library/Application Support/Zhilian'
    return Path(os.getenv('XDG_DATA_HOME', str(home / '.local/share'))) / 'zhilian'


def configure() -> Path:
    root = user_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / '.write-test'
        probe.write_text('', encoding='utf-8')
        probe.unlink()
    except OSError:
        # Some managed Windows environments deny writes under LOCALAPPDATA.
        # Keep the portable app usable and fall back to the user's temp area.
        root = Path(tempfile.gettempdir()) / 'Zhilian'
        root.mkdir(parents=True, exist_ok=True)
    config = Path(os.getenv('ZHILIAN_CONFIG_FILE', str(root / '.env'))).expanduser()
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        with open(config, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write('# Zhilian local configuration; never share API keys.\n'
                    'ZHILIAN_LLM_MODE=hybrid\n'
                    'DEEPSEEK_API_KEY=\nZHILIAN_LOCAL_RERANKER_ENABLED=0\n'
                    'ZHILIAN_QUOTA_PER_CLIENT=60\nZHILIAN_QUOTA_GLOBAL=600\n')
    if os.name == 'posix':
        config.chmod(0o600)
    os.environ['ZHILIAN_CONFIG_FILE'] = str(config)
    for raw in config.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    defaults = {'ZHILIAN_HOST': '127.0.0.1', 'ZHILIAN_PORT': '8765',
                'ZHILIAN_DATA_DIR': str(root / 'data'), 'ZHILIAN_OUTPUT_DIR': str(root / 'exports'),
                'ZHILIAN_LOCAL_RERANKER_DEVICE': 'cpu'}
    for key, value in defaults.items():
        if not os.environ.get(key, '').strip():
            os.environ[key] = value
    # Resolve paths relative to the user configuration file.  A model bundle
    # may also be unpacked beside the frozen executable (portable Windows,
    # Linux, or macOS distribution); discover that location automatically so
    # users do not need to edit an environment variable by hand.
    sibling_model = Path(sys.executable).resolve().parent / 'models' / 'bge-reranker-v2-m3-onnx-int8'
    if sibling_model.is_dir() and not os.environ.get('ZHILIAN_LOCAL_RERANKER_PATH'):
        os.environ.setdefault('ZHILIAN_LOCAL_RERANKER_ENABLED', '1')
        os.environ['ZHILIAN_LOCAL_RERANKER_PATH'] = str(sibling_model)
    for key in ('ZHILIAN_DATA_DIR', 'ZHILIAN_OUTPUT_DIR', 'ZHILIAN_LOCAL_RERANKER_PATH'):
        if os.environ.get(key):
            path = Path(os.environ[key]).expanduser()
            os.environ[key] = str(path if path.is_absolute() else config.parent / path)
    if not os.environ.get('ZHILIAN_LOCAL_RERANKER_PATH') and os.environ.get('ZHILIAN_LOCAL_RERANKER_ENABLED', '').lower() in {'1', 'true', 'on'}:
        os.environ['ZHILIAN_LOCAL_RERANKER_PATH'] = str(root / 'models/bge-reranker-v2-m3-onnx-int8')
    return root


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description='Zhilian local workbench')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--browser', action='store_true', help='Open the workbench in the system browser')
    parser.add_argument('--mcp', action='store_true', help='Run the read-only MCP stdio server')
    args = parser.parse_args()
    root = configure()
    if args.mcp:
        raise SystemExit('MCP stdio server is distributed in the source package; use python -m zhilian.mcp_server.')
    host = os.environ['ZHILIAN_HOST']
    if host not in ('127.0.0.1', 'localhost', '::1') and not os.getenv('ZHILIAN_ACCESS_PASSWORD'):
        raise SystemExit('Public/LAN binding requires ZHILIAN_ACCESS_PASSWORD.')
    import uvicorn
    from zhilian.app import app
    port = int(os.environ['ZHILIAN_PORT'])
    url = f'http://{"[::1]" if host == "::1" else "127.0.0.1"}:{port}'

    # Frozen desktop packages use pywebview so the user sees an application
    # window rather than a browser tab.  Browser mode remains available for
    # environments without WebView2/GTK/Cocoa support.
    desktop_mode = not args.browser and not args.no_browser and os.getenv('ZHILIAN_DESKTOP_MODE', 'app').lower() not in {'browser', 'web'}
    webview = None
    if desktop_mode:
        try:
            import webview as _webview
            webview = _webview
        except Exception as exc:
            print(f'Embedded window unavailable ({exc}); falling back to the system browser.')

    def wait_ready() -> bool:
        for _ in range(80):
            try:
                urllib.request.urlopen(url + '/api/health', timeout=.5).close()
                return True
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        return False

    def run_embedded() -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level='info')
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name='zhilian-server', daemon=True)
        thread.start()
        wait_ready()
        try:
            window = webview.create_window('知链 · 跨文档结论验证', url, width=1440, height=920,
                                           min_size=(1080, 700), text_select=True)
            window.events.closed += lambda: setattr(server, 'should_exit', True)
            webview.start(debug=False)
        except Exception as exc:
            print(f'Embedded window failed ({exc}); opening the system browser instead.')
            webbrowser.open(url)
            thread.join()
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    if webview is not None:
        run_embedded()
        return

    def open_ready():
        for _ in range(60):
            try:
                urllib.request.urlopen(url + '/api/health', timeout=.5).close()
                webbrowser.open(url)
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    webbrowser.open(url)
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.25)

    if not args.no_browser and os.getenv('ZHILIAN_NO_BROWSER', '').lower() not in {'1', 'true'}:
        threading.Thread(target=open_ready, daemon=True).start()
    print(f'Zhilian: {url}\nConfiguration: {os.environ["ZHILIAN_CONFIG_FILE"]}\nData: {root}')
    uvicorn.run(app, host=host, port=port, log_level='info')


if __name__ == '__main__':
    main()
