from zhilian.demo import create_demo
from zhilian.store import Store
from zhilian import agent
from zhilian import review


def approve_all(store, ws):
    return store.confirm(ws['id'], ws['revision'], [
        {'claim_id': c['id'], 'refs': c['refs']} for c in ws['claims']
    ])


def test_cross_document_audit_is_read_only_evidence(tmp_path):
    store = Store(tmp_path / 'data')
    ws = store.create('跨文档审计', create_demo(tmp_path / 'files'))
    ws = approve_all(store, ws)
    revision = ws['revision']
    result = store.cross_document_audit(ws['id'], revision)
    assert result['cross_audit']['signature']
    assert result['cross_audit']['groups']
    assert result['revision'] == revision
    assert result['cross_audit']['findings'] == []
    assert not result.get('last_repair')


def test_repair_plan_contains_risk_and_does_not_write(tmp_path):
    store = Store(tmp_path / 'data')
    ws = store.create('修复规划', create_demo(tmp_path / 'files'))
    ws = approve_all(store, ws)
    ws = store.change(ws['id'], ws['revision'], {'sales_current': 90})
    result = store.build_repair_plan(ws['id'], ws['revision'])
    assert result['repair_plan']['requires_approval'] is True
    assert result['repair_plan']['summary']['total'] > 0
    item = result['repair_plan']['items'][0]
    assert {'file_id', 'location', 'before', 'after', 'risk_level', 'verification'} <= item.keys()
    assert result['last_repair'] is None


def test_review_plan_becomes_stale_after_data_change(tmp_path):
    store = Store(tmp_path / 'data')
    ws = store.create('审计快照', create_demo(tmp_path / 'files'))
    raw = store.read(ws['id'])
    raw['cross_audit'] = review.audit_documents(raw)
    store.write(raw)
    ws = store.change(raw['id'], raw['revision'], {'sales_current': 90})
    assert ws['cross_audit']['stale'] is True
    assert ws['cross_audit']['signature'] != review.signature(ws)


def test_semantic_audit_is_metadata_only_and_logged(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    store = Store(tmp_path / 'data')
    ws = store.create('语义审计', create_demo(tmp_path / 'files'))
    ws = approve_all(store, ws)
    seen = {}

    def fake_semantic(groups):
        seen['groups'] = groups
        claims = groups[0]['claims']
        by_file = {}
        for item in claims:
            by_file.setdefault(item['file_id'], item['claim_id'])
        return [{'claim_ids': list(by_file.values())[:2],
                 'category': 'semantic_scope_risk', 'reason': '不同文件对同一业务指标使用了不同表述'}]

    monkeypatch.setattr('zhilian.llm.suggest_cross_document_findings', fake_semantic)
    result = store.cross_document_audit(ws['id'], ws['revision'])
    audit = result['cross_audit']
    assert audit['semantic_status'] == 'completed'
    assert any(item['source'] == 'model' for item in audit['findings'])
    assert result['model_calls'][-1]['node'] == 'audit_agent'
    assert all('value' not in item for group in seen['groups'] for item in group['claims'])
    assert all('test-secret' not in str(item) for item in seen['groups'])


def test_review_agent_endpoints_leave_trace_and_files_unchanged(tmp_path):
    from zhilian import agent
    store = Store(tmp_path / 'data')
    ws = store.create('审计智能体', create_demo(tmp_path / 'files'))
    ws = approve_all(store, ws)
    before = {d['id']: (store.folder(ws['id']) / store.read(ws['id'])['generation'] /
                        next(x['stored_name'] for x in store.read(ws['id'])['documents'] if x['id'] == d['id'])).read_bytes()
              for d in ws['documents']}
    ws = agent.run_cross_document_audit(store, ws['id'], ws['revision'])
    ws = agent.run_repair_plan(store, ws['id'], ws['revision'])
    nodes = [entry['node'] for entry in ws['agent']['trace']]
    assert 'cross_document_audit' in nodes and 'plan_repair' in nodes
    current = store.read(ws['id'])
    for document in current['documents']:
        assert (store.folder(ws['id']) / current['generation'] / document['stored_name']).read_bytes() == before[document['id']]

