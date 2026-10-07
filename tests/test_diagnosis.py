import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from zhilian import app as app_module, llm
from zhilian.demo import create_demo
from zhilian.diagnose import CATEGORY, diagnose, template_explanation
from zhilian.engine import check, inspect
from zhilian.office import digest
from zhilian.store import Store


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(app_module, 'load_environment', lambda: None)
    monkeypatch.setenv('DEEPSEEK_API_KEY', '')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'deepseek-flash')
    monkeypatch.setenv('ZHILIAN_ACCESS_PASSWORD', '')


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path / 'data')
    return store, store.create('诊断测试', create_demo(tmp_path / 'files'), demo=True)


@pytest.fixture
def api_project(tmp_path):
    app = app_module.create_app(tmp_path / 'api')
    with TestClient(app) as client:
        ws = client.post('/api/projects/demo').json()
        yield client, app.state.store, ws


def model_response(explanations):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
        {'explanations': explanations}, ensure_ascii=False)}}]})


def file_hashes(store, ws):
    return {d['id']: digest(store.folder(ws['id']) / ws['generation'] / d['stored_name'])
            for d in ws['documents']}


def test_codes_preserve_legacy_fields(project):
    store, ws = project
    before = deepcopy(ws)
    checks, summary = inspect(ws)
    assert summary['consistent'] == 11
    for claim, result in zip(ws['claims'], checks):
        assert result['code'] == result['status'] == 'consistent'
        assert result['expected'] == claim['original']
        assert result['reason']
        assert result['evidence'] == [{k: f.get(k) for k in (
            'id', 'subject', 'metric', 'period', 'value', 'unit', 'scope', 'sheet', 'cell')}
            for ref in claim['refs'] for f in ws['facts'] if f['id'] == ref]
    quote = next(c for c in ws['claims'] if c['kind'] == 'quote')
    result = check(quote, ws['facts'])
    assert result['reason'].endswith('万元 = 125 万元')
    assert result['expected'] == '本期销售额为125万元'
    changed = deepcopy(ws['facts'])
    next(f for f in changed if f['id'] == 'sales_current')['value'] = 90
    result = check(quote, changed)
    assert result['status'] == result['code'] == 'inconsistent'
    assert result['expected'] == '本期销售额为90万元'
    assert result['reason'].endswith('万元 = 90 万元')
    assert ws == before
    assert {'checks', 'summary', 'unmatched_segments', 'diagnosis', 'diagnosis_summary'} <= store.public(ws).keys()


@pytest.mark.parametrize('case,code,reason', [
    ('empty', 'missing_data', '来源缺失或数值不可计算'),
    ('null', 'missing_data', '来源缺失或数值不可计算'),
    ('missing_ref', 'missing_data', "'missing-id'"),
    ('quote_count', 'source_count', '数值引用必须关联一个事实'),
    ('growth_short', 'missing_data', '增长率需要上期、本期两个事实'),
    ('growth_extra', 'source_count', '增长率需要上期、本期两个事实'),
    ('rank_short', 'source_conflict', '排名至少需要两个对象'),
    ('rank_duplicate', 'source_conflict', '排名对象重复，请核对比较集合'),
    ('chart_count', 'source_conflict', '图表关联数量与原始类别不一致'),
    ('scope', 'scope_mismatch', '增长率的主体、指标、口径或上期本期顺序不一致'),
    ('order', 'scope_mismatch', '增长率的主体、指标、口径或上期本期顺序不一致'),
    ('chart_period', 'scope_mismatch', '图表类别与来源期间不匹配'),
    ('zero', 'needs_review', '上期数值必须大于零；零值或负基期转人工复核'),
    ('negative', 'needs_review', '上期数值必须大于零；零值或负基期转人工复核'),
    ('unsupported', 'needs_review', '不支持的论断类型'),
])
def test_anomaly_classification(project, case, code, reason):
    _, ws = project
    facts = deepcopy(ws['facts'])
    byid = {f['id']: f for f in facts}
    kind = ('growth' if case.startswith('growth') or case in ('scope', 'order', 'zero', 'negative')
            else 'ranking' if case.startswith('rank') else 'chart' if case.startswith('chart') else 'quote')
    claim = deepcopy(next(c for c in ws['claims'] if c['kind'] == kind))
    if case == 'empty':
        claim['refs'] = []
    elif case == 'null':
        byid[claim['refs'][0]]['value'] = None
    elif case == 'missing_ref':
        claim['refs'] = ['missing-id']
    elif case == 'quote_count':
        claim['refs'] += ['sales_prev']
    elif case in ('growth_short', 'rank_short', 'chart_count'):
        claim['refs'] = claim['refs'][:1]
    elif case == 'growth_extra':
        claim['refs'] += ['product_a']
    elif case == 'rank_duplicate':
        byid['product_b']['subject'] = byid['product_a']['subject']
    elif case == 'scope':
        byid['sales_current']['scope'] = '其他口径'
    elif case == 'order':
        claim['refs'].reverse()
    elif case == 'chart_period':
        byid['sales_current']['period'] = '下一期'
    elif case in ('zero', 'negative'):
        byid['sales_prev']['value'] = 0 if case == 'zero' else -10
    elif case == 'unsupported':
        claim['kind'] = 'unknown'
    result = check(claim, facts)
    assert result['code'] == code
    assert result['status'] == 'unverifiable'
    assert result['expected'] is None
    assert result['reason'] == reason
    records, summary = diagnose(dict(ws, claims=[claim]), [result])
    assert records[0]['category'] == CATEGORY[code]
    assert records[0]['explanation'] and records[0]['fix_hint']
    assert summary['source_conflict' if code == 'source_count' else code] == 1


def test_evidence_anchors_and_purity(project, monkeypatch):
    _, ws = project
    checks, _ = inspect(ws)
    before = deepcopy((ws, checks))
    def forbidden(*args):
        pytest.fail('诊断不应重新计算')
    monkeypatch.setattr('zhilian.engine.check', forbidden)
    records, summary = diagnose(ws, checks)
    assert (ws, checks) == before
    assert summary['total'] == len(checks)
    assert {r['file_name'] for r in records} == {'分析报告.docx', '业务汇报.pptx'}
    for record, claim, checked in zip(records, ws['claims'], checks):
        assert record['evidence'] is checked['evidence']
        assert all(f['sheet'] == '事实表' and f['cell'].startswith('E') for f in record['evidence'])
        assert record['label'] == claim['label']
        assert record['location'] == claim['location']
        assert record['start'] == claim.get('start') and record['end'] == claim.get('end')
        if claim['kind'] != 'chart':
            block = next(b for b in ws['blocks'] if (b['file_id'], b['location']) == (claim['file_id'], claim['location']))
            assert block['text'][record['start']:record['end']] == record['original']
        assert record['explanation'] == template_explanation(record)
        assert record['expected'] is None


def test_no_model_templates_and_no_http(api_project, monkeypatch):
    client, store, ws = api_project
    def forbidden(*args, **kwargs):
        pytest.fail('未配置模型时不应发送请求')
    monkeypatch.setattr(httpx, 'post', forbidden)
    before = store.read(ws['id'])
    hashes = file_hashes(store, before)
    response = client.post(f'/api/projects/{ws["id"]}/diagnosis/explain', json={'revision': ws['revision']})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    result = response.json()
    assert result['diagnosis_explanations'] == {}
    assert all(r['explanation'] == template_explanation(r) for r in result['diagnosis'])
    assert '未调用模型' in result['audit'][-1]['detail']
    after = store.read(ws['id'])
    assert {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)} == {
        'revision', 'audit', 'diagnosis_explanations'}
    assert file_hashes(store, after) == hashes


def test_deepseek_success_metadata_only_and_revision(api_project, monkeypatch):
    client, store, ws = api_project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    first = ws['claims'][0]['id']
    def fake_post(url, **kwargs):
        assert url == 'https://api.deepseek.com/chat/completions'
        assert kwargs['headers']['Authorization'] == 'Bearer test-secret'
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        assert 'test-secret' not in json.dumps(payload)
        for record in payload['records']:
            assert set(record) == {'claim_id', 'kind', 'original', 'code', 'category', 'reason', 'expected', 'evidence'}
            assert all(set(f) == {'id', 'subject', 'metric', 'period', 'unit', 'scope', 'sheet', 'cell'} for f in record['evidence'])
        return model_response([{'claim_id': first, 'explanation': '引用与来源一致。'}])
    monkeypatch.setattr(httpx, 'post', fake_post)
    before = store.read(ws['id'])
    hashes = file_hashes(store, before)
    endpoint = f'/api/projects/{ws["id"]}/diagnosis/explain'
    response = client.post(endpoint, json={'revision': ws['revision']})
    assert response.status_code == 200
    result = response.json()
    assert result['diagnosis_explanations'] == {first: '引用与来源一致。'}
    assert result['audit'][-1]['event'] == '诊断解释'
    assert 'deepseek-flash' in result['audit'][-1]['detail']
    assert 'test-secret' not in response.text
    after = store.read(ws['id'])
    assert 'test-secret' not in json.dumps(after)
    assert {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)} == {
        'revision', 'audit', 'diagnosis_explanations', 'model_calls'}
    assert after['model_calls'][-1]['node'] == 'diagnosis_agent'
    assert file_hashes(store, after) == hashes
    assert result['checks'] == ws['checks']
    stale = client.post(endpoint, json={'revision': ws['revision']})
    assert stale.status_code == 400 and '其他窗口' in stale.text
    assert store.read(ws['id']) == after


@pytest.mark.parametrize('failure', ['timeout', 'http', 'invalid_json', 'invalid_structure'])
def test_model_failure_safe_and_unchanged(api_project, monkeypatch, failure):
    client, store, ws = api_project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    before = store.read(ws['id'])
    hashes = file_hashes(store, before)
    def fake_post(*args, **kwargs):
        if failure == 'timeout':
            raise httpx.ReadTimeout('test-secret provider-internal-error')
        if failure == 'http':
            return httpx.Response(401, text='test-secret provider-internal-error')
        if failure == 'invalid_json':
            return httpx.Response(200, json={'choices': [{'message': {'content': 'test-secret provider-internal-error'}}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"explanations":null}'}}]})
    monkeypatch.setattr(httpx, 'post', fake_post)
    response = client.post(f'/api/projects/{ws["id"]}/diagnosis/explain', json={'revision': ws['revision']})
    assert response.status_code == 400
    assert 'test-secret' not in response.text and 'provider-internal-error' not in response.text
    assert store.read(ws['id']) == before
    assert file_hashes(store, before) == hashes
    assert client.get(f'/api/projects/{ws["id"]}').json()['diagnosis'] == ws['diagnosis']


def test_untrusted_explanations_are_filtered(project, monkeypatch):
    _, ws = project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    first = ws['claims'][0]['id']
    values = [None, {}, 1, 'test-secret', '结果为999999999万元。', 'sk-othersecret123']
    for value in values:
        monkeypatch.setattr(httpx, 'post', lambda *a, **k: model_response([
            {'claim_id': first, 'explanation': value},
            {'claim_id': [], 'explanation': '无效'}, {'claim_id': 'unknown', 'explanation': '无效'}]))
        assert llm.explain_diagnosis(ws['diagnosis']) == {}


@pytest.mark.parametrize('phase', ['before', 'during'])
def test_file_hash_guard(api_project, monkeypatch, phase):
    client, store, ws = api_project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    state = store.read(ws['id'])
    path = store.folder(ws['id']) / state['generation'] / state['documents'][0]['stored_name']
    def tamper():
        with path.open('ab') as out:
            out.write(b'changed')
    def fake_post(*args, **kwargs):
        assert phase == 'during', 'SHA 校验失败后不应调用模型'
        tamper()
        return model_response([])
    monkeypatch.setattr(httpx, 'post', fake_post)
    if phase == 'before':
        tamper()
    response = client.post(f'/api/projects/{ws["id"]}/diagnosis/explain', json={'revision': ws['revision']})
    assert response.status_code == 400 and '源文件' in response.text
    assert store.read(ws['id']) == state


def test_revision_race_does_not_overwrite_changes(api_project, monkeypatch):
    client, store, ws = api_project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    def fake_post(*args, **kwargs):
        store.change(ws['id'], ws['revision'], {'sales_current': 90})
        return model_response([])
    monkeypatch.setattr(httpx, 'post', fake_post)
    response = client.post(f'/api/projects/{ws["id"]}/diagnosis/explain', json={'revision': ws['revision']})
    assert response.status_code == 400 and '其他窗口' in response.text
    result = store.public(store.read(ws['id']))
    assert result['summary']['inconsistent'] > 0
    assert result['audit'][-1]['event'] == '数据变更'


def test_explanation_invalidated_by_confirm_update_repair_undo(api_project, monkeypatch):
    client, store, ws = api_project
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    first = ws['claims'][0]['id']
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: model_response([{'claim_id': first, 'explanation': '这是当前版本的解释。'}]))
    def explained(w):
        response = client.post(f'/api/projects/{w["id"]}/diagnosis/explain', json={'revision': w['revision']})
        assert response.status_code == 200
        result = response.json()
        assert result['diagnosis_explanations']
        return result
    ws = explained(ws)
    ws = store.confirm(ws['id'], ws['revision'], [{'claim_id': c['id'], 'refs': c['refs']} for c in ws['claims']])
    assert ws['diagnosis_explanations'] == {}
    ws = explained(ws)
    ws = store.change(ws['id'], ws['revision'], {'sales_current': 90})
    assert ws['diagnosis_explanations'] == {} and ws['diagnosis_summary']['inconsistent'] > 0
    ws = explained(ws)
    ids = [r['claim_id'] for r in ws['checks'] if r['status'] == 'inconsistent']
    ws = store.repair(ws['id'], ws['revision'], ids)
    assert ws['diagnosis_explanations'] == {} and ws['diagnosis_summary']['inconsistent'] == 0
    ws = store.undo(ws['id'], ws['revision'])
    assert ws['diagnosis_explanations'] == {} and ws['diagnosis_summary']['inconsistent'] > 0


def test_endpoint_security(api_project, monkeypatch):
    client, store, ws = api_project
    endpoint = f'/api/projects/{ws["id"]}/diagnosis/explain'
    payload = {'revision': ws['revision']}
    response = client.post(endpoint, json=payload, headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    monkeypatch.setenv('ZHILIAN_ACCESS_PASSWORD', 'local-password')
    assert client.post(endpoint, json=payload).status_code == 401
    response = client.post(endpoint, json=payload, auth=('zhilian', 'local-password'))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
