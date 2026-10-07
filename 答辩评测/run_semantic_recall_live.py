"""Run the opt-in semantic claim recall against an isolated demo workspace.

The script uses the configured DeepSeek endpoint and never prints or stores
the API key. It creates only a temporary evaluation workspace under output/.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zhilian import agent, llm, reranker  # noqa: E402
from zhilian.demo import create_demo  # noqa: E402
from zhilian.store import Store  # noqa: E402


def main() -> int:
    config = llm.config()
    if not config['enabled']:
        print('未配置 DEEPSEEK_API_KEY；请在当前 shell 或服务器 .env 中配置后重试。')
        return 2
    os.environ['ZHILIAN_SEMANTIC_RECALL'] = '1'
    os.environ.setdefault('ZHILIAN_LLM_MODE', 'hybrid')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    root = ROOT / 'output' / f'semantic_recall_live_{stamp}'
    files = root / 'files'
    store = Store(root / 'state')
    try:
        ws = store.create('语义论断召回实测', create_demo(files, semantic_demo=True), demo=True)
        result = agent.run_agent(store, ws['id'], ws['revision'])
        report = {
            'workspace': result['id'],
            'mode': result.get('model_mode'),
            'model': config['model'],
            'reranker_enabled': reranker.status()['enabled'],
            'semantic_candidates': result.get('agent', {}).get('semantic_candidates', []),
            'model_call_summary': result.get('model_call_summary'),
            'model_calls': result.get('model_calls', []),
            'trace': result.get('agent', {}).get('trace', []),
            'note': '候选仅用于预览，未自动并入正式论断或修改 Office 文件。',
        }
        report_path = root / 'semantic_recall_report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(report_path),
                          'candidate_count': len(report['semantic_candidates']),
                          'model_call_summary': report['model_call_summary']},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        # Keep the report but remove the isolated uploaded Office fixtures and
        # workspace state; this test must never look like a production project.
        shutil.rmtree(root / 'files', ignore_errors=True)
        shutil.rmtree(root / 'state', ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
