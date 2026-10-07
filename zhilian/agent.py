"""证据链验证智能体：确定性状态机编排，复用现有模块作为工具。

Agent 是编排器/大脑，不是让大模型自由选工具的 ReAct 循环。
模型仅提出候选；计算、确认、文件修复分别复用 engine / store / office。
业务副作用只能经 decide 的人工闸门。轨迹和审计本身属于任务元数据。
"""
from copy import deepcopy
import os
import re
from time import perf_counter
from uuid import uuid4

from . import engine, llm, quota, reranker, document_profile
from .store import now
from . import review


def list_facts(store, ws, payload):
    return {'facts': [{k: f[k] for k in ('id', 'subject', 'metric', 'period', 'unit', 'scope', 'sheet', 'cell')}
                      for f in ws['facts']]}


def list_claims(store, ws, payload):
    return {'claims': [{k: c[k] for k in ('id', 'kind', 'original', 'refs', 'confirmed', 'label')}
                       for c in ws['claims']]}


def check_claim(store, ws, payload):
    claim = next(c for c in ws['claims'] if c['id'] == payload['claim_id'])
    if llm.mode(ws) == 'api_only' and not claim['confirmed'] and claim['kind'] != 'chart':
        claim = dict(claim, refs=[])
    return engine.check(claim, ws['facts'])


def propose_links(store, ws, payload):
    return {'links': [{'claim_id': c['id'], 'refs': c['refs']} for c in ws['claims'] if not c['confirmed']]}


def suggest_links_llm(store, ws, payload):
    eligible = [c for c in ws['claims'] if not c['confirmed'] and c['kind'] != 'chart']
    claim_ids = payload.get('claim_ids') if isinstance(payload, dict) else None
    if isinstance(claim_ids, list):
        allowed = {cid for cid in claim_ids if isinstance(cid, str)}
        eligible = [c for c in eligible if c['id'] in allowed]
    if not llm.config()['enabled'] or not eligible:
        return {'suggestions': []}
    return {'suggestions': llm.suggest_links(eligible, ws['facts'])}


def suggest_links_local(store, ws, payload):
    """Rank deterministic candidates and semantically recall zero-candidate claims.

    A zero lexical hit is exactly where a reranker adds value.  The full fact
    table is scored only when it is small enough for predictable latency; large
    tables are left for the remote/API or human fallback.
    """
    if llm.mode(ws) not in {'local', 'hybrid'} or not reranker.status()['enabled']:
        return {'suggestions': []}
    suggestions, decisions = [], {}
    stats = {'zero_seen': 0, 'zero_scored': 0, 'multi_scored': 0, 'abstained': 0}
    max_zero_facts = max(1, int(os.getenv('ZHILIAN_LOCAL_ZERO_MAX_FACTS', '150')))
    requested = payload.get('claim_ids') if isinstance(payload, dict) else None
    allowed = {cid for cid in requested if isinstance(cid, str)} if isinstance(requested, list) else None
    eligible = [c for c in ws['claims'] if not c['confirmed'] and c['kind'] != 'chart'
                and (allowed is None or c['id'] in allowed)][:40]
    for claim in eligible:
        started = perf_counter()
        candidates = engine.best_facts(claim['original'], ws['facts'])
        zero_claim = len(candidates) == 0
        if zero_claim:
            stats['zero_seen'] += 1
            if len(ws['facts']) > max_zero_facts:
                decisions[claim['id']] = {'action': 'abstain', 'reason': '事实表超过本地零候选预算',
                                         'candidates': len(ws['facts']), 'duration_ms': 0}
                continue
            candidates = ws['facts']
            stats['zero_scored'] += 1
        elif len(candidates) == 1:
            checked = engine.check(dict(claim, refs=[candidates[0]['id']]), ws['facts'])
            if checked['status'] == 'unverifiable':
                decisions[claim['id']] = {'action': 'abstain', 'refs': [],
                                         'reason': checked['reason'], 'candidates': 1,
                                         'duration_ms': round((perf_counter()-started)*1000)}
                stats['abstained'] += 1
            continue
        else:
            stats['multi_scored'] += 1
        scores = reranker.score_pairs(claim['original'], candidates)
        choice = reranker.choose(scores)
        if choice['action'] == 'link':
            checked = engine.check(dict(claim, refs=choice['refs']), ws['facts'])
            if checked['status'] == 'unverifiable':
                choice = dict(choice, action='abstain', refs=[], reason=checked['reason'])
        decisions[claim['id']] = dict(choice, candidates=len(candidates),
                                      duration_ms=round((perf_counter()-started)*1000))
        if choice['action'] == 'link':
            suggestions.append({'claim_id': claim['id'], 'refs': choice['refs'],
                                'source': 'local-reranker',
                                'reason': ('本地 reranker 对零候选事实表完成语义召回并通过阈值'
                                           if zero_claim else
                                           '本地 reranker 在收紧后的候选中排序通过阈值')})
        else:
            stats['abstained'] += 1
    return {'suggestions': suggestions, 'stats': stats, 'decisions': decisions}


def suggest_semantic_claims(store, ws, payload):
    """Recall potentially claim-bearing sentences without creating claims."""
    gaps = engine.unmatched_spans(ws.get('blocks', []), ws.get('claims', []))
    recalled = llm.suggest_claim_sentences(gaps)
    by_key = {f'{b.get("file_id")}::{b.get("location")}': b for b in gaps}
    result = []
    for item in recalled:
        block = by_key.get(item['block_id'])
        if not block:
            continue
        sentences = list(re.finditer(r'[^。！？；;\n]+', block['text']))
        if item['sentence_index'] >= len(sentences):
            continue
        match = sentences[item['sentence_index']]
        text = match.group().strip(' \t\r\n，,、')
        if not text:
            continue
        leading = len(match.group()) - len(match.group().lstrip(' \t\r\n，,、'))
        start = int(block.get('start', 0)) + match.start() + leading
        result.append({'file_id': block['file_id'], 'location': block['location'],
                       'start': start, 'end': start + len(text), 'text': text,
                       'reason': item['reason'], 'extraction': '语义召回候选'})
    return {'candidates': result}


def _candidate_buckets(ws, rules):
    """Separate deterministic, ambiguous and uncovered claims before models run.

    The deterministic engine remains the authority.  A claim with an existing
    rule candidate is never sent to a model merely because a model is enabled.
    Local ranking is reserved for genuinely multi-candidate text; remote LLM
    fallback is reserved for zero candidates or local abstentions.
    """
    byid = {r['claim_id']: r['refs'] for r in rules}
    unique, multi, zero = [], [], []
    for claim in ws['claims']:
        if claim['confirmed'] or claim['kind'] == 'chart':
            continue
        rule_refs = byid.get(claim['id'], [])
        lexical = engine.best_facts(claim['original'], ws['facts'])
        checked = engine.check(dict(claim, refs=rule_refs), ws['facts'])
        rule_verifiable = bool(rule_refs) and checked['status'] != 'unverifiable'
        # Compound assertions (growth/ranking/budget) are already resolved by
        # typed deterministic rules and must not be split by a reranker.
        if rule_verifiable and claim['kind'] in {'growth', 'ranking', 'threshold', 'chart'}:
            unique.append(claim)
        elif len(lexical) == 1 and rule_verifiable:
            unique.append(claim)
        elif lexical:
            multi.append(claim)
        else:
            zero.append(claim)
    return unique, multi, zero


def confirm_links(store, ws, payload):
    return store.confirm(ws['id'], ws['revision'], payload['items'])


def repair(store, ws, payload):
    return store.repair(ws['id'], ws['revision'], [i['claim_id'] for i in payload['items']])


def cross_document_audit(store, ws, payload):
    """Read-only deterministic + optional semantic cross-document review."""
    return review.audit_documents(ws)


def plan_repair(store, ws, payload):
    """Read-only repair planning; approval and Office writes stay separate."""
    return review.repair_plan(ws)


# 扩展工具只需注册同签名函数，并在需要的状态节点通过 _tool 调用。
# 不开放客户端任意工具执行入口；歧义分析、总结可在此追加只读工具。
TOOLS = {'list_facts': list_facts, 'list_claims': list_claims, 'check_claim': check_claim,
         'propose_links': propose_links, 'suggest_links_llm': suggest_links_llm,
         'suggest_links_local': suggest_links_local,
         'suggest_semantic_claims': suggest_semantic_claims,
         'cross_document_audit': cross_document_audit, 'plan_repair': plan_repair,
         'confirm_links': confirm_links, 'repair': repair}
MUTATIONS = {'confirm_links', 'repair'}


def _write(store, ws):
    ws['agent']['revision'] = ws['revision']
    store.write(ws)


def _ensure_review_agent(ws):
    """Create the minimal trace container for review-only agent actions."""
    if not isinstance(ws.get('agent'), dict):
        ws['agent'] = {'run_id': uuid4().hex[:16], 'phase': 'idle', 'started_at': now(),
                       'pending': None, 'trace': [], 'model_mode': llm.mode(ws),
                       'result': {'consistent': 0, 'inconsistent': 0, 'unverifiable': 0, 'repaired': 0}}
    ws['agent'].setdefault('trace', [])
    ws['agent'].setdefault('review_trace', [])


def _review_trace(store, ws, node, summary):
    _ensure_review_agent(ws)
    entry = {'node': node, 'at': now(), 'summary': summary, 'model': None, 'duration_ms': 0}
    ws['agent']['trace'].append(entry)
    ws['agent']['review_trace'].append(entry)
    ws['audit'].append({'time': entry['at'], 'event': 'Agent 执行',
                        'detail': f'{node}：{summary}'})
    ws['agent']['revision'] = ws['revision']


def run_cross_document_audit(store, wid, revision):
    """Run the review agent and persist only its evidence snapshot."""
    with store.lock:
        ws = store.read(wid)
        store.verify(ws, revision)
        _ensure_review_agent(ws)
        _review_trace(store, ws, 'cross_document_audit', '跨文档审计智能体开始；只读证据模式')
        store.write(ws)
        try:
            result = store.cross_document_audit(wid, revision)
        except Exception:
            ws = store.read(wid)
            _review_trace(store, ws, 'cross_document_audit', '审计失败；原文件与来源未改变')
            store.write(ws)
            raise
        ws = store.read(wid)
        audit = ws.get('cross_audit') or {}
        _review_trace(store, ws, 'cross_document_audit',
                      f'审计完成；归并 {len(audit.get("groups", []))} 组，发现 {len(audit.get("findings", []))} 项疑点')
        store.write(ws)
        return store.public(ws)


def run_repair_plan(store, wid, revision):
    """Run the approval-gated repair planning agent without writing Office files."""
    with store.lock:
        ws = store.read(wid)
        store.verify(ws, revision)
        _ensure_review_agent(ws)
        _review_trace(store, ws, 'plan_repair', '修复规划智能体开始；等待人工批准，不写回文件')
        store.write(ws)
        try:
            result = store.build_repair_plan(wid, revision)
        except Exception:
            ws = store.read(wid)
            _review_trace(store, ws, 'plan_repair', '修复计划生成失败；原文件未改变')
            store.write(ws)
            raise
        ws = store.read(wid)
        plan = ws.get('repair_plan') or {}
        _review_trace(store, ws, 'plan_repair',
                      f'计划完成；{plan.get("summary", {}).get("total", 0)} 项待批准')
        store.write(ws)
        return store.public(ws)


def _trace(store, ws, node, summary, model=None, duration_ms=0, commit=True):
    entry = {'node': node, 'at': now(), 'summary': summary, 'model': model, 'duration_ms': duration_ms}
    ws['agent']['trace'].append(entry)
    ws['audit'].append({'time': entry['at'], 'event': 'Agent 执行',
                        'detail': f'{node}：{summary}' + (f'；模型 {model}' if model else '')})
    if commit:
        _write(store, ws)


def _tool(store, ws, name, payload=None, *, approved=False, commit=True):
    """执行一个登记在 TOOLS 里的工具，并把开始/结束两条轨迹写入工作区。

    commit=False 时只更新内存中的轨迹，不落盘——调用方负责在批量结束后写一次。
    诊断阶段对**每条论断**调一次 check_claim，而这里原本每次会写两遍完整的
    state.json（500 条论断 = 1000 次全量序列化）；轨迹内容完全一样，只是写得太频繁。
    失败路径仍强制落盘，保证"工具执行失败"留痕不会因为延迟写而丢失。
    """
    if name in MUTATIONS and not approved:
        raise ValueError('此工具需要人工批准')
    model = (llm.config()['model'] if name in {'suggest_links_llm', 'suggest_semantic_claims'}
             else ('local-reranker' if name == 'suggest_links_local' else None))
    _trace(store, ws, name, '开始执行工具', model, commit=commit)
    trace_index, audit_index = len(ws['agent']['trace']) - 1, len(ws['audit']) - 1
    started = perf_counter()
    try:
        result = TOOLS[name](store, ws, payload or {})
    except Exception:
        if name in {'suggest_links_llm', 'suggest_semantic_claims'}:
            # Do not let a failed request's metrics leak into the next model
            # call in the same worker context.
            metrics = llm.consume_last_call_metrics()
            store.append_model_call(ws, metrics,
                                    node='extraction_agent' if name == 'suggest_semantic_claims' else 'link_agent',
                                    mode_value=llm.mode(ws))
        if name in MUTATIONS:
            # 工具可能已持久化人工决定；失败收尾不能用旧副本覆盖它。
            refreshed = store.read(ws['id'])
            ws.clear()
            ws.update(refreshed)
        entry = ws['agent']['trace'][trace_index]
        entry.update(summary='工具执行失败，未记录外部错误原文', duration_ms=round((perf_counter()-started)*1000))
        ws['audit'][audit_index]['detail'] = f'{name}：工具执行失败'
        _write(store, ws)   # 失败必须立刻留痕，不受 commit 影响
        raise
    if name in MUTATIONS:
        # store 返回的是 public 视图；持久化必须重新读取含 stored_name 的原状态。
        refreshed = store.read(ws['id'])
        ws.clear()
        ws.update(refreshed)
    summary = '工具执行完成'
    if name == 'suggest_links_llm':
        summary = f'收到 {len(result["suggestions"])} 项模型候选，尚未人工确认'
        metrics = llm.consume_last_call_metrics()
        if metrics:
            store.append_model_call(ws, metrics, node='link_agent', mode_value=llm.mode(ws),
                                    candidates_returned=len(result.get('suggestions', [])))
    elif name == 'suggest_semantic_claims':
        summary = f'召回 {len(result["candidates"])} 条语义论断候选，尚未并入正式论断'
        metrics = llm.consume_last_call_metrics()
        if metrics:
            store.append_model_call(ws, metrics, node='extraction_agent', mode_value=llm.mode(ws),
                                    candidates_returned=len(result.get('candidates', [])))
    elif name == 'suggest_links_local':
        summary = f'收到 {len(result["suggestions"])} 项本地 reranker 候选，尚未人工确认'
        metrics = {'provider': 'local-reranker', 'model': reranker.status().get('model_name', 'local-reranker'),
                       'duration_ms': round((perf_counter() - started) * 1000),
                       'outcome': 'succeeded', 'prompt_tokens': 0,
                       'completion_tokens': 0, 'total_tokens': 0,
                   'estimated_cost_cny': 0.0}
        store.append_model_call(ws, metrics, node='link_agent', mode_value=llm.mode(ws),
                                candidates_returned=len(result.get('suggestions', [])))
    entry = ws['agent']['trace'][trace_index]
    entry.update(summary=summary, duration_ms=round((perf_counter()-started)*1000))
    ws['audit'][audit_index]['detail'] = f'{name}：{summary}' + (f'；模型 {model}' if model else '')
    if commit:
        _write(store, ws)
    return result


def _anchors(ws):
    blocks = {(b['file_id'], b['location']): b['text'] for b in ws['blocks']}
    for c in ws['claims']:
        if c['kind'] == 'chart':
            # 原生图表使用 office 提供的对象位置锚点，不伪造文本字符区间。
            if not c.get('location') or not any(d['id'] == c['file_id'] and d['kind'] == 'pptx' for d in ws['documents']):
                raise ValueError('缺少图表锚点')
            continue
        text = blocks.get((c['file_id'], c['location']), '')
        if not (0 <= c['start'] < c['end'] <= len(text)) or text[c['start']:c['end']] != c['original']:
            raise ValueError('原文锚点无效')


def _remembered(claim, opt_refs, facts, rules):
    if not isinstance(rules, list):
        return False
    byid = {f['id']: f for f in facts}
    index = {r['key']: r for r in rules if isinstance(r, dict) and isinstance(r.get('key'), str)}
    for fid in opt_refs:
        fact = byid.get(fid)
        if not fact:
            return False
        parts = [claim.get('kind'), fact.get('subject'), fact.get('metric'), fact.get('period')]
        if not all(isinstance(v, str) and v for v in parts):
            return False
        rule = index.get('|'.join(parts))
        if not rule or rule.get('fact_id') != fid:
            return False
    return bool(opt_refs)


def _options(claim, facts, refs, suggestions, rules=None, *, include_rules=True):
    options = []
    known = {f['id'] for f in facts}

    def add(candidate, reason):
        if not candidate or any(i not in known for i in candidate):
            return
        candidate = list(dict.fromkeys(candidate))
        if any(o['refs'] == candidate for o in options):
            return
        checked = engine.check(dict(claim, refs=candidate), facts)
        remembered = include_rules and _remembered(claim, candidate, facts, rules)
        options.append({'refs': candidate,
                        'reason': ('复用已确认规则（上次同类论断选择此来源）· ' if remembered else '') + reason,
                        'status': checked['status'], 'remembered': remembered})

    if include_rules:
        add(refs, '现有规则候选，请核对主体、期间和口径')
    for suggestion in suggestions:
        if suggestion['claim_id'] == claim['id']:
            # 不把模型自由文本复制到审计或决策提示，避免回显外部敏感内容。
            source = '本地 reranker' if suggestion.get('source') == 'local-reranker' else 'DeepSeek'
            add(suggestion['refs'], f'{source} 提出的来源候选，仍需人工核对')
    if include_rules and not refs:
        # 规则没给出候选时不能让界面进入"零选项"死结：人工必须至少有一个可勾
        # 选项，否则 decide 会以"请选择真实存在的来源事实"失败，项目直接卡住。
        if claim['kind'] == 'growth':
            add((claim.get('spec') or {}).get('suggested_refs') or [],
                '增长率需要同口径的上期与本期，请核对配对')
            for metric, pair in engine.growth_pairs(facts)[:20]:
                add(pair, f'增长率需要同口径的上期与本期；候选指标「{metric}」，请核对是否本文所指')
        for fact in engine.best_facts(claim['original'], facts):
            add([fact['id']], '规则发现可能来源，请比较统计口径')
    options.sort(key=lambda o: not o.get('remembered', False))
    return options


def _item(ws, c, checked=None):
    checked = checked or engine.check(c, ws['facts'])
    return {'claim_id': c['id'], 'kind': c['kind'], 'original': c['original'], 'refs': list(c['refs']),
            'file_id': c['file_id'], 'label': c['label'], 'reason': checked['reason'],
            'evidence': checked['evidence'], 'expected': checked['expected']}


def _diagnose(store, ws):
    started = perf_counter()
    diagnostics = []
    # commit=False：逐条核验只更新内存轨迹，诊断小结写完后再统一落盘一次。
    # 原实现对每条论断写两遍完整 state.json，是这一阶段最大的固定开销。
    for c in ws['claims']:
        checked = _tool(store, ws, 'check_claim', {'claim_id': c['id']}, commit=False)
        diagnostics.append(dict(_item(ws, c, checked), status=checked['status'], confirmed=c['confirmed']))
    checked_ws = ws
    if llm.mode(ws) == 'api_only':
        checked_ws = dict(ws, claims=[dict(c, refs=[]) if not c['confirmed'] and c['kind'] != 'chart'
                                     else c for c in ws['claims']])
    _, summary = engine.inspect(checked_ws)
    ws['agent']['result'].update({k: summary[k] for k in ('consistent', 'inconsistent', 'unverifiable')})
    ws['agent']['diagnostics'] = diagnostics
    _trace(store, ws, 'diagnose', f'一致 {summary["consistent"]} 项，不一致 {summary["inconsistent"]} 项，无法判断 {summary["unverifiable"]} 项',
           duration_ms=round((perf_counter()-started)*1000))   # 这一条会落盘，上面的轨迹一并写入


def _advance(store, ws):
    _anchors(ws)
    if 'candidates' not in ws['agent']:
        started = perf_counter()
        # 这三步是纯只读的盘点，中间不需要落盘；本段末尾的 _trace('link') 会一次性
        # 把它们的轨迹写下去。任一步抛错时 _tool 的失败分支仍会强制落盘留痕。
        _tool(store, ws, 'list_facts', commit=False)
        _tool(store, ws, 'list_claims', commit=False)
        semantic_candidates = []
        semantic_enabled = os.getenv('ZHILIAN_SEMANTIC_RECALL', '').strip().lower() in {'1', 'true', 'on'}
        if semantic_enabled and llm.mode(ws) in {'hybrid', 'api'} and llm.config()['enabled']:
            gate = quota.check()
            if gate['allowed']:
                quota.consume()
                try:
                    semantic_result = _tool(store, ws, 'suggest_semantic_claims', commit=False)
                    semantic_candidates = semantic_result.get('candidates', [])
                except Exception:
                    ws['audit'].append({'time': now(), 'event': 'Agent 语义召回降级',
                                        'detail': '语义论断召回不可用，保留规则抽取结果'})
            else:
                _trace(store, ws, 'model_skipped', f'语义论断召回未执行：{gate["reason"]}', commit=False)
        ws['agent']['semantic_candidates'] = semantic_candidates
        rules = _tool(store, ws, 'propose_links', commit=False)['links']
        suggestions = []
        unique, multi, zero = _candidate_buckets(ws, rules)
        routing_mode = ws['agent'].get('model_mode', llm.mode(ws))
        local_ids = set()
        local_decisions = {}
        local_stats = {'zero_seen': 0, 'zero_scored': 0, 'multi_scored': 0, 'abstained': 0}
        local_enabled = routing_mode in {'local', 'hybrid'} and reranker.status()['enabled']
        local_unavailable = routing_mode in {'local', 'hybrid'} and not local_enabled
        api_batches = 0
        if local_enabled and (multi or zero):
            try:
                difficult = multi + zero
                for start in range(0, len(difficult), 40):
                    local_result = _tool(store, ws, 'suggest_links_local',
                                         {'claim_ids': [c['id'] for c in difficult[start:start + 40]]})
                    local = local_result['suggestions']
                    local_decisions.update(local_result.get('decisions', {}))
                    for key, value in (local_result.get('stats') or {}).items():
                        if key in local_stats:
                            local_stats[key] += value
                    suggestions.extend(local)
                    local_ids.update(item['claim_id'] for item in local if item.get('refs'))
            except Exception:
                local_unavailable = True
                ws['audit'].append({'time': now(), 'event': 'Agent 本地模型降级',
                                    'detail': '本地 reranker 不可用，困难样本转远程模型或人工'})
        elif local_unavailable and (multi or zero):
            ws['audit'].append({'time': now(), 'event': 'Agent 本地模型降级',
                                'detail': '本地模型未安装或未启用；保留规则候选，按当前模式转 API 或人工'})
        # api_only deliberately includes unique rule matches; other online
        # routes only send unresolved claims.
        if routing_mode == 'api_only':
            api_claims = [c for c in ws['claims'] if not c['confirmed'] and c['kind'] != 'chart']
        elif routing_mode in {'api', 'hybrid'}:
            api_claims = ([c for c in zero if c['id'] not in local_ids]
                          + [c for c in multi if c['id'] not in local_ids])
        else:
            api_claims = []
        gate = quota.check()
        skip = ''
        if routing_mode == 'rules':
            skip = '当前为 rules 模式；未发起本地或 API 模型请求'
        elif routing_mode == 'local':
            skip = '当前为 local 模式；不使用 API，未发起远程请求'
        elif not llm.config()['enabled']:
            skip = '未配置模型密钥；未发起模型请求'
        elif not api_claims:
            skip = '规则与本地候选已覆盖全部论断；未发起模型请求'
        elif len(ws['facts']) > 150:
            skip = '事实数超过单次模型关联上限150；未发起模型请求'
        elif not gate['allowed']:
            skip = gate['reason']
        quota_blocked = gate['reason'] if api_claims and skip == gate['reason'] and not gate['allowed'] else ''
        api_error = False
        if not skip:
            try:
                # Keep each request within the provider contract while still
                # batching the difficult claims. A normal project therefore
                # needs one request for <=40 claims, two for 41-80, etc.
                for start in range(0, len(api_claims), 40):
                    # 额度可能在一批中途被用尽（同一访客并发或全局预算触顶），
                    # 此时停止后续批次并降级，已拿到的候选保留。
                    if not quota.check()['allowed']:
                        quota_blocked = quota.check()['reason']
                        break
                    batch = api_claims[start:start + 40]
                    api_batches += 1
                    quota.consume()
                    remote = _tool(store, ws, 'suggest_links_llm',
                                   {'claim_ids': [c['id'] for c in batch]})['suggestions']
                    suggestions.extend(remote)
            except Exception:
                api_error = True
                ws['audit'].append({'time': now(), 'event': 'Agent 模型降级',
                                    'detail': ('API 不可用；纯 API 对照保留人工选择，不补规则候选'
                                               if routing_mode == 'api_only' else
                                               '困难样本模型不可用，继续使用规则候选，来源未自动确认')})
            if quota_blocked:
                ws['audit'].append({'time': now(), 'event': '模型额度用尽',
                                    'detail': quota_blocked})
        else:
            if quota_blocked:
                # 额度触顶单独记一条审计：运营与答辩都需要一眼看出"这次为什么没走模型"，
                # 而不是混在通用的 Agent 执行记录里。
                ws['audit'].append({'time': now(), 'event': '模型额度用尽', 'detail': skip})
            _trace(store, ws, 'model_skipped', skip)
        ws['agent']['model_routing'] = {
            'mode': routing_mode, 'mode_label': llm.mode_label(routing_mode),
            'document_profile': ws['agent'].get('document_profile'),
            'model_profile': ws['agent'].get('model_profile'),
            'escalation_policy': {
                'rules_unique': '规则候选唯一且确定性核验通过，不升级模型',
                'local_gate': '本地分数与分差达到对应模型阈值，且数值、单位、主体、期间和口径核验通过',
                'api_gate': '本地模型不可用、低置信、或确定性核验未通过时才升级 API',
                'manual_gate': 'API 不可用或仍无法证明时转人工，不自动确认或写回',
            },
            'rule_route_claims': len(unique) if routing_mode != 'api_only' else 0,
            'local_unavailable': local_unavailable, 'api_error': api_error,
            'unique_rule_claims': len(unique), 'multi_candidate_claims': len(multi),
            'zero_candidate_claims': len(zero), 'local_reranker_suggestions': len(local_ids),
            'local_zero_recall_claims': local_stats['zero_scored'],
            'local_multi_rank_claims': local_stats['multi_scored'],
            'local_abstentions': local_stats['abstained'],
            'api_candidate_claims': len(api_claims), 'api_batches': api_batches, 'api_suggestions': sum(
                1 for item in suggestions if item.get('claim_id') not in local_ids),
            'api_escalation_reasons': {
                'local_abstain_or_validation_failed': sum(
                    1 for cid, decision in local_decisions.items()
                    if cid not in local_ids and decision.get('action') == 'abstain'),
                'local_unavailable': int(local_unavailable and bool(api_claims)),
                'api_mode_requested': int(routing_mode == 'api'),
                'api_only_requested': int(routing_mode == 'api_only'),
            },
            'model_quota': quota.snapshot(), 'quota_blocked': quota_blocked, 'api_skip_reason': skip,
        }
        api_ids = {s['claim_id'] for s in suggestions if s.get('source') != 'local-reranker' and s.get('refs')}
        unique_ids = {c['id'] for c in unique}
        ws['agent']['claim_routes'] = {}
        for c in ws['claims']:
            if c['confirmed']:
                continue
            local = local_decisions.get(c['id'], {})
            source = ('rules' if (routing_mode != 'api_only' and c['id'] in unique_ids) or c['kind'] == 'chart'
                      else 'local' if c['id'] in local_ids else 'api' if c['id'] in api_ids else 'human')
            ws['agent']['claim_routes'][c['id']] = {
                'source': source, 'top_raw_score': local.get('top_raw_score'), 'margin': local.get('margin'),
                'local_duration_ms': local.get('duration_ms'), 'local_action': local.get('action'),
                'reason': ('规则来源明确，等待人工确认' if source == 'rules' else
                           'API 返回来源候选，等待人工确认' if source == 'api' else
                           local.get('reason') or '没有可用模型候选，请人工指定来源'),
            }
        # In api_only, rule candidates are not treated as an automatic result;
        # the API suggestion (or an explicit human choice) is the only route
        # presented as a model result.  Other modes retain the deterministic
        # rule candidate as the first auditable option.
        byid = {r['claim_id']: r['refs'] for r in rules}
        candidates = {}
        for c in ws['claims']:
            if c['confirmed']:
                continue
            include_rules = routing_mode != 'api_only' or c['kind'] == 'chart'
            refs = byid.get(c['id'], []) if include_rules else []
            options = _options(c, ws['facts'], refs, suggestions, ws.get('link_rules', []),
                               include_rules=include_rules)
            ambiguous = not refs or len(options) != 1 or engine.check(c, ws['facts'])['status'] == 'unverifiable'
            candidates[c['id']] = dict(_item(ws, dict(c, refs=refs)), options=options,
                                      decision_kind='resolve_ambiguity' if ambiguous else 'confirm_links')
        ws['agent']['candidates'] = candidates
        _trace(store, ws, 'link', f'已整理 {len(candidates)} 项候选；图表仅使用规则来源，全部等待人工确认',
               duration_ms=round((perf_counter()-started)*1000))
    _diagnose(store, ws)
    for kind in ('resolve_ambiguity', 'confirm_links'):
        items = [ws['agent']['candidates'][c['id']] for c in ws['claims']
                 if not c['confirmed'] and c['id'] in ws['agent']['candidates']
                 and ws['agent']['candidates'][c['id']]['decision_kind'] == kind]
        if items:
            return _ask(store, ws, kind, items)
    repairs = [_item(ws, c) for c in ws['claims'] if c['confirmed'] and engine.check(c, ws['facts'])['status'] == 'inconsistent']
    if repairs:
        return _ask(store, ws, 'approve_repair', repairs)
    _trace(store, ws, 'verify', '已重新执行确定性核验；无法判断项仍保留为人工复核，不计为通过')
    ws['agent'].update(phase='done', pending=None)
    result = ws['agent']['result']
    _trace(store, ws, 'done', f'任务结束：修复 {result["repaired"]} 项，无法判断 {result["unverifiable"]} 项')
    return _finish(store, ws)


def _ask(store, ws, kind, items):
    ws['agent'].update(phase='awaiting_decision', pending={'kind': kind, 'items': items})
    _trace(store, ws, 'ask', f'等待人工决定 {len(items)} 项；关闭窗口后可继续')
    return _finish(store, ws)


def _finish(store, ws):
    ws['revision'] += 1
    _write(store, ws)
    return store.public(ws)


def _fail(store, wid):
    ws = store.read(wid)
    ws['agent'].update(phase='idle', pending=None, error='智能体执行失败；已完成的人工决定保留，请刷新后重试')
    ws['audit'].append({'time': now(), 'event': 'Agent 失败', 'detail': ws['agent']['error']})
    return _finish(store, ws)


def run_agent(store, wid, revision):
    with store.lock:
        ws = store.read(wid)
        with llm.using_mode(llm.mode(ws)):
            return _run_agent(store, wid, revision)


def _run_agent(store, wid, revision):
    # 首版同步、单进程运行，持锁完成一次有限状态推进，避免网络返回后覆盖新版本。
    with store.lock:
        ws = store.read(wid)
        store.verify(ws, revision)
        previous = ws.get('agent', {})
        fresh = (previous.get('revision') == revision
                 and previous.get('model_mode', 'hybrid') == llm.mode(ws))
        if fresh and previous.get('phase') == 'awaiting_decision':
            return store.public(ws)
        if not (fresh and previous.get('phase') == 'running'):
            ws['agent'] = {'run_id': uuid4().hex[:16], 'phase': 'running', 'started_at': now(),
                           'pending': None, 'trace': [], 'model_mode': llm.mode(ws),
                           'result': {'consistent': 0, 'inconsistent': 0, 'unverifiable': 0, 'repaired': 0}}
        ws['revision'] += 1
        _trace(store, ws, 'run', '开始或恢复证据链验证任务；业务修改须人工批准')
        try:
            started = perf_counter()
            engine.inspect(ws)
            _anchors(ws)
            _trace(store, ws, 'prepare', '工作区版本、文件校验和原文锚点检查通过',
                   duration_ms=round((perf_counter()-started)*1000))
            profile = document_profile.detect(ws.get('documents', []), ws.get('blocks', []))
            try:
                annual_ready = reranker.status('annual')
            except TypeError:
                # Keep lightweight test doubles and older integrations compatible.
                annual_ready = reranker.status()
            selected = 'annual' if profile['profile'] == 'annual' and annual_ready['enabled'] else 'default'
            ws['agent']['document_profile'] = profile
            ws['agent']['model_profile'] = {
                'selected': selected,
                'requested': profile['profile'],
                'fallback': profile['profile'] == 'annual' and selected != 'annual',
                'model_name': (annual_ready if selected == 'annual' else reranker.status()).get('model_name'),
                'reason': ('年报特征明确且年报模型可用' if selected == 'annual' else
                           '年报模型不可用，回退默认本地模型' if profile['profile'] == 'annual' else
                           '未达到年报识别阈值，使用默认本地模型'),
            }
            with reranker.using_profile(selected):
                return _advance(store, ws)
        except Exception:
            return _fail(store, wid)


def decide(store, wid, revision, decisions):
    with store.lock:
        ws = store.read(wid)
        with llm.using_mode(llm.mode(ws)):
            return _decide(store, wid, revision, decisions)


def _decide(store, wid, revision, decisions):
    with store.lock:
        ws = store.read(wid)
        store.verify(ws, revision)
        agent = ws.get('agent', {})
        if (agent.get('revision') != revision
                or agent.get('model_mode', 'hybrid') != llm.mode(ws)):
            raise ValueError('项目已在其他窗口更新，请刷新后重试')
        pending = agent.get('pending')
        if agent.get('phase') != 'awaiting_decision' or not pending:
            raise ValueError('当前没有等待批准的智能体决定')
        if not isinstance(decisions, list) or len(decisions) != 1:
            raise ValueError('请只提交当前阶段的一组决定')
        decision = decisions[0]
        if not isinstance(decision, dict) or decision.get('kind') != pending['kind']:
            raise ValueError('决定类型与当前待办不一致')
        items = decision.get('items')
        if not isinstance(items, list) or not items:
            raise ValueError('请选择至少一项明确批准；也可关闭窗口稍后继续')
        allowed = {i['claim_id'] for i in pending['items']}
        ids = [i.get('claim_id') for i in items if isinstance(i, dict)]
        if len(ids) != len(items) or any(not isinstance(i, str) or i not in allowed for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('决定包含重复或不属于当前待办的论断')
        kind = decision['kind']
        if kind != 'approve_repair':
            known = {f['id'] for f in ws['facts']}
            claims = {c['id']: c for c in ws['claims']}
            for item in items:
                refs = item.get('refs')
                if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in known for r in refs):
                    raise ValueError('请选择真实存在的来源事实')
                checked = engine.check(dict(claims[item['claim_id']], refs=refs), ws['facts'])
                if checked['status'] == 'unverifiable':
                    raise ValueError('当前选择无法通过确定性校验，请核对来源、顺序与口径')
        try:
            _anchors(ws)
            agent.update(phase='running', pending=None)
            ws['revision'] += 1
            _trace(store, ws, 'decide', f'用户明确批准 {len(items)} 项：{kind}')
            _tool(store, ws, 'repair' if kind == 'approve_repair' else 'confirm_links', {'items': items}, approved=True)
            if kind == 'approve_repair':
                ws['agent']['result']['repaired'] += len(items)
                checks, _ = engine.inspect(ws)
                if any(c['status'] != 'consistent' for c in checks if c['claim_id'] in ids) or not ws['last_repair']['verified']:
                    raise ValueError('修复复核未通过')
                _trace(store, ws, 'verify', '重新读取 Office 后修复项均一致，其他受支持正文未变')
            return _advance(store, ws)
        except Exception:
            return _fail(store, wid)


def agent_status(store, wid):
    with store.lock:
        ws = store.read(wid)
        agent = ws.get('agent', {})
        result = {k: deepcopy(agent.get(k, default)) for k, default in
                  (('phase', 'idle'), ('pending', None), ('trace', []), ('result', {}))}
        result['stale'] = bool(agent) and (agent.get('revision') != ws['revision']
                                         or agent.get('model_mode', 'hybrid') != llm.mode(ws))
        result['model_mode'] = llm.mode(ws)
        return result
