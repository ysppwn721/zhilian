import json

from zhilian import agent, reranker
from zhilian.demo import create_demo
from zhilian.store import Store


def test_semantic_demo_zero_candidate_uses_local_reranker(tmp_path, monkeypatch):
    """线上语义夹具必须真实走一次本地零候选召回。"""
    monkeypatch.setattr(reranker, 'status', lambda: {'enabled': True})

    def score_pairs(text, facts, **kwargs):
        # 模拟 BGE 把“营收”召回到事实表的“销售额—本期”。
        rows = []
        for fact in facts:
            raw = 5.0 if fact['id'] == 'sales_current' else -5.0
            rows.append({'fact_id': fact['id'], 'raw_score': raw,
                         'score': 0.99 if raw > 0 else 0.01})
        return sorted(rows, key=lambda row: row['raw_score'], reverse=True)

    monkeypatch.setattr(reranker, 'score_pairs', score_pairs)
    store = Store(tmp_path / 'store')
    ws = store.create('语义演示', create_demo(tmp_path / 'files', semantic_demo=True), demo=True)
    ws = agent.run_agent(store, ws['id'], ws['revision'])
    routing = ws['agent']['model_routing']
    assert routing['zero_candidate_claims'] == 1
    assert routing['local_zero_recall_claims'] == 1
    assert routing['local_reranker_suggestions'] == 1
    assert routing['api_candidate_claims'] == 0
    item = next(i for i in ws['agent']['pending']['items'] if '营收' in i['original'])
    assert item['options'][0]['refs'] == ['sales_current']


def test_semantic_demo_query_is_opt_in_for_compatibility(tmp_path):
    from fastapi.testclient import TestClient
    from zhilian.app import create_app

    client = TestClient(create_app(tmp_path / 'api'))
    plain = client.post('/api/projects/demo').json()
    semantic = client.post('/api/projects/demo?semantic=1').json()
    assert not any('营收' in c['original'] for c in plain['claims'])
    assert any('营收' in c['original'] for c in semantic['claims'])


def test_semantic_recall_rebuilds_original_span_locally(tmp_path, monkeypatch):
    store = Store(tmp_path / 'store')
    ws = store.create('语义召回', create_demo(tmp_path / 'files'))
    block = dict(ws['blocks'][0], text='公司实现营收125万元。普通说明。', start=0, end=18)
    ws['blocks'] = [block]
    ws['claims'] = []
    monkeypatch.setattr(agent.llm, 'suggest_claim_sentences',
                        lambda blocks: [{'block_id': f"{blocks[0]['file_id']}::{blocks[0]['location']}",
                                         'sentence_index': 0, 'reason': '语义结论'}])
    result = agent.suggest_semantic_claims(store, ws, {})
    assert result['candidates'][0]['text'] == '公司实现营收125万元'
    assert result['candidates'][0]['start'] == 0


def test_semantic_recall_is_explicitly_opt_in_and_audited(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    monkeypatch.setenv('ZHILIAN_SEMANTIC_RECALL', '1')
    monkeypatch.setattr(agent.llm, 'suggest_claim_sentences',
                        lambda blocks: [])
    store = Store(tmp_path / 'store')
    ws = store.create('语义召回开关', create_demo(tmp_path / 'files'))
    ws = agent.run_agent(store, ws['id'], ws['revision'])
    assert ws['agent']['semantic_candidates'] == []
    assert any(t['node'] == 'suggest_semantic_claims' for t in ws['agent']['trace'])
    assert ws['model_call_summary']['remote'] == 0  # monkeypatched classifier made no HTTP call
