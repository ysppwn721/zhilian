import json
from copy import deepcopy

import httpx
import pytest

from zhilian.demo import create_demo
from zhilian.store import Store
from zhilian.engine import check, extract_claims
from zhilian import llm


@pytest.fixture(autouse=True)
def deepseek_environment(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_MODEL', raising=False)


def test_qualitative_growth_preserved_and_invalidated(tmp_path):
    store = Store(tmp_path/'data')
    w = store.create('test', create_demo(tmp_path/'files'))
    text = '本期销售额较上期增长。'
    c = extract_claims({'file_id':'f','location':'x','label':'test','text':text}, w['facts'])[0]
    assert c['spec']['qualitative']
    facts = deepcopy(w['facts'])
    fact = next(f for f in facts if f['id']=='sales_current')
    fact['value'] = 120
    assert check(c,facts)['status']=='consistent'
    fact['value'] = 90
    assert check(c,facts)['expected']=='本期销售额较上期下降'


def test_model_disabled(monkeypatch):
    monkeypatch.setenv('ZHILIAN_LLM_BASE_URL','https://example.test/v1')
    monkeypatch.setenv('ZHILIAN_LLM_MODEL','legacy-model')
    monkeypatch.setenv('ZHILIAN_LLM_API_KEY','legacy-secret')
    assert not llm.config()['enabled']
    with pytest.raises(ValueError, match='尚未配置'):
        llm.suggest_links([],[])


def test_model_suggestions_validated_without_key_leak(monkeypatch,tmp_path):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    store=Store(tmp_path/'data')
    w=store.create('test',create_demo(tmp_path/'files'))
    first=w['claims'][0]
    def fake_post(url,**kwargs):
        assert url=='https://api.deepseek.com/chat/completions'
        assert kwargs['headers']['Authorization']=='Bearer test-secret'
        assert kwargs['json']['model']=='deepseek-flash'
        assert kwargs['json']['thinking']=={'type':'disabled'}
        assert kwargs['json']['response_format']=={'type':'json_object'}
        assert kwargs['json']['stream'] is False
        assert kwargs['follow_redirects'] is False
        # Fact values are not needed for semantic linking and are not sent.
        user=json.loads(kwargs['json']['messages'][1]['content'])
        assert 'value' not in user['facts'][0]
        assert all(c['expected_k'] == (2 if c['kind'] == 'growth' else 1)
                   for c in user['claims'] if c['kind'] in {'growth', 'quote'})
        assert all(c['task_rule'] in {'growth_set', 'quote_current'}
                   for c in user['claims'] if c['kind'] in {'growth', 'quote'})
        content={'suggestions':[
            {'claim_id':first['id'],'refs':first['refs'],'reason':'match'},
            {'claim_id':first['id'],'refs':['invented-id'],'reason':'bad'},
            {'claim_id':'nonexistent','refs':[],'reason':'bad'}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content)}}]})
    monkeypatch.setattr(httpx,'post',fake_post)
    result=llm.suggest_links(w['claims'],w['facts'])
    assert len(result)==1
    assert first['confirmed'] is False
    assert 'test-secret' not in json.dumps(llm.config())


def test_model_call_metrics_capture_usage_without_payload_or_key(monkeypatch, tmp_path):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    monkeypatch.setenv('ZHILIAN_INPUT_PRICE_PER_MILLION', '1')
    monkeypatch.setenv('ZHILIAN_OUTPUT_PRICE_PER_MILLION', '2')
    store = Store(tmp_path / 'data')
    w = store.create('metrics', create_demo(tmp_path / 'files'))
    first = w['claims'][0]

    def fake_post(url, **kwargs):
        content = {'suggestions': [{'claim_id': first['id'], 'refs': first['refs'], 'reason': 'match'}]}
        return httpx.Response(200, json={
            'choices': [{'message': {'content': json.dumps(content)}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
        })

    monkeypatch.setattr(httpx, 'post', fake_post)
    result = llm.suggest_links(w['claims'], w['facts'])
    metrics = llm.consume_last_call_metrics()
    assert len(result) == 1
    assert metrics['outcome'] == 'succeeded'
    assert metrics['prompt_tokens'] == 11 and metrics['completion_tokens'] == 7
    assert metrics['total_tokens'] == 18 and metrics['estimated_cost_cny'] == 0.000025
    assert 'test-secret' not in json.dumps(metrics)


def test_semantic_sentence_recall_returns_only_validated_sentence_keys(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    blocks = [{'file_id': 'doc', 'location': 'p0', 'text': '公司实现营收125万元。普通说明。'}]

    def fake_post(url, **kwargs):
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        assert payload['blocks'][0]['block_id'] == 'doc::p0'
        assert '125' not in payload['blocks'][0]['text']
        body = {'sentences': [
            {'block_id': 'doc::p0', 'sentence_index': 0, 'reason': '包含数量结论'},
            {'block_id': 'invented', 'sentence_index': 0, 'reason': 'discard'},
            {'block_id': 'doc::p0', 'sentence_index': '0', 'reason': 'discard'},
        ]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(body)}}],
                                  'usage': {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14}})

    monkeypatch.setattr(httpx, 'post', fake_post)
    result = llm.suggest_claim_sentences(blocks)
    assert result == [{'block_id': 'doc::p0', 'sentence_index': 0, 'reason': '包含数量结论'}]
    assert llm.consume_last_call_metrics()['total_tokens'] == 14


def test_model_error_does_not_expose_provider_body(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    monkeypatch.setattr(httpx,'post',lambda *args,**kwargs:httpx.Response(401,text='secret-body'))
    c={'id':'c','kind':'quote','original':'x','refs':[],'confirmed':False}
    with pytest.raises(ValueError) as exc:
        llm.suggest_links([c],[])
    assert '401' in str(exc.value)
    assert 'secret-body' not in str(exc.value)


@pytest.mark.parametrize('body', [
    {'choices':[]},
    {'choices':[{'message':{'content':None}}]},
    {'choices':[{'message':{'content':'[]'}}]},
    {'choices':[{'message':{'content':'{"suggestions":null}'}}]},
    {'choices':[{'message':{'content':'not-json'}}]},
])
def test_invalid_deepseek_response_is_reported(monkeypatch,body):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    monkeypatch.setattr(httpx,'post',lambda *args,**kwargs:httpx.Response(200,json=body))
    c={'id':'c','kind':'quote','original':'x','refs':[],'confirmed':False}
    with pytest.raises(ValueError,match='返回结构无效'):
        llm.suggest_links([c],[])


def test_deepseek_timeout_keeps_safe_error(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    def timeout(*args,**kwargs):
        raise httpx.ReadTimeout('test-secret')
    monkeypatch.setattr(httpx,'post',timeout)
    c={'id':'c','kind':'quote','original':'x','refs':[],'confirmed':False}
    with pytest.raises(ValueError,match='连接失败') as exc:
        llm.suggest_links([c],[])
    assert 'test-secret' not in str(exc.value)


def test_deepseek_web_workflow(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from zhilian import app as app_module
    monkeypatch.setattr(app_module,'load_environment',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    monkeypatch.setenv('ZHILIAN_ACCESS_PASSWORD','')
    client=TestClient(app_module.create_app(tmp_path/'data'))
    health=client.get('/api/health').json()
    # 这里刻意用**完整字典相等**：任何新增字段都必须被有意识地审阅，
    # 避免有人不小心把密钥或内部路径加到 health 里。
    assert health['model']=={'enabled':True,'model':'deepseek-flash',
                             'provider':'DeepSeek','base_url':'https://api.deepseek.com',
                             'redact_numbers':False,'key_configured':True}
    assert 'test-secret' not in json.dumps(health)
    w=client.post('/api/projects/demo').json()
    first=w['claims'][0]
    content={'suggestions':[{'claim_id':first['id'],'refs':first['refs'],'reason':'match'}]}
    monkeypatch.setattr(httpx,'post',lambda *args,**kwargs:httpx.Response(
        200,json={'choices':[{'message':{'content':json.dumps(content)}}]}))
    response=client.post(f"/api/projects/{w['id']}/suggest",json={'revision':w['revision']})
    assert response.status_code==200
    updated=response.json()
    assert updated['suggestions']==content['suggestions']
    assert updated['revision']==w['revision']+1
    assert updated['claims']==w['claims']
    assert updated['documents']==w['documents']
    assert 'test-secret' not in response.text
