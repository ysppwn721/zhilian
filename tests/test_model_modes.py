from zhilian import agent, llm, quota, reranker
from zhilian.demo import create_demo
from zhilian.store import Store


def _project(tmp_path, semantic=True):
    store = Store(tmp_path / 'data')
    return store, store.create('模式测试', create_demo(tmp_path / 'files', semantic_demo=semantic), True)


def test_mode_validation_and_health(monkeypatch):
    for value in ('rules', 'local', 'hybrid', 'api', 'api_only'):
        monkeypatch.setenv('ZHILIAN_LLM_MODE', value)
        assert llm.mode() == value
    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'unknown')
    assert llm.mode() == 'hybrid'
    assert llm.mode_label()


def test_health_exposes_mode_without_key_leak(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from zhilian.app import create_app
    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'local')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    health = TestClient(create_app(tmp_path / 'api')).get('/api/health').json()
    assert health['model_mode'] == 'local'
    assert health['model_mode_label'] == '规则 + 本地语义模型'
    assert 'test-secret' not in str(health)


def test_rules_mode_skips_local_and_api(tmp_path, monkeypatch):
    store, ws = _project(tmp_path)
    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'rules')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setattr(reranker, 'status', lambda: {'enabled': True})
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_local', lambda *args: (_ for _ in ()).throw(AssertionError('local called')))
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_llm', lambda *args: (_ for _ in ()).throw(AssertionError('api called')))
    result = agent.run_agent(store, ws['id'], ws['revision'])
    assert result['agent']['model_routing']['mode'] == 'rules'
    assert result['agent']['model_routing']['api_batches'] == 0


def test_local_mode_skips_api(tmp_path, monkeypatch):
    store, ws = _project(tmp_path)
    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'local')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setattr(reranker, 'status', lambda: {'enabled': True})
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_local', lambda *args: {
        'suggestions': [], 'stats': {'zero_scored': 0, 'multi_scored': 0, 'abstained': 0}
    })
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_llm', lambda *args: (_ for _ in ()).throw(AssertionError('api called')))
    result = agent.run_agent(store, ws['id'], ws['revision'])
    assert result['agent']['model_routing']['mode'] == 'local'
    assert result['agent']['model_routing']['api_batches'] == 0


def test_api_mode_skips_local_and_api_only_includes_unique(tmp_path, monkeypatch):
    quota.configure(tmp_path / 'quota.json')
    store, ws = _project(tmp_path, semantic=True)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setenv('ZHILIAN_QUOTA_PER_CLIENT', '10')
    monkeypatch.setenv('ZHILIAN_QUOTA_GLOBAL', '10')
    local_calls = []
    api_calls = []
    monkeypatch.setattr(reranker, 'status', lambda: {'enabled': True})
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_local', lambda *args: local_calls.append(1) or {'suggestions': [], 'stats': {}})
    monkeypatch.setitem(agent.TOOLS, 'suggest_links_llm', lambda _store, _ws, payload: api_calls.append(payload) or {'suggestions': []})

    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'api')
    result = agent.run_agent(store, ws['id'], ws['revision'])
    assert not local_calls
    assert api_calls
    assert result['agent']['model_routing']['mode'] == 'api'

    # A fresh project makes the api_only comparison independent of prior state.
    store2, ws2 = _project(tmp_path / 'second', semantic=True)
    api_calls.clear()
    monkeypatch.setenv('ZHILIAN_LLM_MODE', 'api_only')
    result2 = agent.run_agent(store2, ws2['id'], ws2['revision'])
    assert not local_calls
    eligible = [c for c in result2['claims'] if not c['confirmed'] and c['kind'] != 'chart']
    assert result2['agent']['model_routing']['api_candidate_claims'] == len(eligible)
    assert result2['agent']['model_routing']['api_batches'] == 1
