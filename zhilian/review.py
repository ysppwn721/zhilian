"""Read-only cross-document review and deterministic repair plans."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from . import engine
from .graph import Derivations, impact


def signature(ws):
    payload = {k: ws.get(k) for k in ('facts', 'claims', 'documents', 'derivations', 'fact_quality', 'model_mode')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def evidence(ws, ids):
    wanted = set(ids)
    docs = {d['id']: d for d in ws['documents']}
    return [{'claim_id': c['id'], 'file_id': c['file_id'],
             'file_name': docs.get(c['file_id'], {}).get('name', ''),
             'label': c['label'], 'location': c['location'],
             'start': c.get('start'), 'end': c.get('end'),
             'original': c['original'], 'confirmed': c['confirmed'], 'refs': list(c['refs'])}
            for c in ws['claims'] if c['id'] in wanted]


def influence(ws, fact_ids):
    return impact(Derivations(ws.get('derivations') or []), ws['claims'], fact_ids)


def _finding(ws, category, ids, fact_ids, reason, *, source='rules', numeric=False):
    return {'category': category, 'source': source, 'requires_review': True,
            'numeric_verified': numeric, 'reason': reason,
            'claim_ids': list(dict.fromkeys(ids)), 'fact_ids': list(dict.fromkeys(fact_ids)),
            'evidence': evidence(ws, ids), 'influence': influence(ws, fact_ids)}


def audit_documents(ws):
    facts = {f['id']: f for f in ws['facts']}
    grouped = defaultdict(list)
    for c in ws['claims']:
        if not c['confirmed'] or c.get('source') == 'ocr':
            continue
        # Compare only quantities, never a growth percentage with its source amount.
        if c['kind'] not in {'quote', 'chart'}:
            continue
        for index, fid in enumerate(c['refs']):
            f = facts.get(fid)
            if not f:
                continue
            key = tuple(str(f.get(k, '')).strip() for k in ('subject', 'metric', 'period'))
            grouped[key].append((c, f, index))
    groups, findings = [], []
    for key, rows in grouped.items():
        ids = list(dict.fromkeys(c['id'] for c, _, _ in rows))
        fact_ids = list(dict.fromkeys(f['id'] for _, f, _ in rows))
        file_ids = set(c['file_id'] for c, _, _ in rows) | {f.get('file_id') for _, f, _ in rows if f.get('file_id')}
        if len(file_ids) < 2:
            continue
        groups.append({'subject': key[0], 'metric': key[1], 'period': key[2],
                       'claim_ids': ids, 'fact_ids': fact_ids, 'file_count': len(file_ids)})
        scopes = {f.get('scope') for _, f, _ in rows}
        if len(scopes) > 1:
            findings.append(_finding(ws, 'scope_risk', ids, fact_ids,
                                     '同一主体、指标与期间引用了不同统计口径，不能直接合并数值。'))
            continue
        mismatches = [c['id'] for c, _, _ in rows if engine.check(c, ws['facts'])['status'] == 'inconsistent']
        if mismatches:
            findings.append(_finding(ws, 'value_mismatch', ids, fact_ids,
                                     '正文或原生图表与已确认的 Excel 来源不一致；差异由确定性引擎计算。', numeric=True))
        # Comparing source facts catches independently linked, conflicting table rows.
        target_unit = rows[0][1].get('unit')
        try:
            values = {engine.convert(f['value'], f['unit'], target_unit) for _, f, _ in rows}
            if len(values) > 1:
                findings.append(_finding(ws, 'source_value_conflict', ids, fact_ids,
                                         '同一主体、指标、期间与口径的事实数值不一致，已按兼容单位换算。', numeric=True))
        except (ValueError, KeyError, TypeError):
            findings.append(_finding(ws, 'unit_risk', ids, fact_ids,
                                     '同指标的数值缺失或单位不兼容，无法进行数值归并。'))
    semantic_groups = build_semantic_groups(ws)
    return {'signature': signature(ws), 'source_revision': ws.get('revision'),
            'groups': groups, 'semantic_groups': semantic_groups, 'findings': findings,
            'confirmed_claims': sum(bool(c['confirmed']) for c in ws['claims']),
            'unconfirmed_claims': sum(not c['confirmed'] for c in ws['claims']),
            'semantic_status': 'not_requested', 'semantic_claims_sent': 0,
            'semantic_claims_total': len(ws['claims']), 'stale': False}


def build_semantic_groups(ws):
    """Build model input groups without sending numeric values.

    Deterministic grouping requires an exact metric name.  The semantic agent
    gets a looser subject/period view so it can flag synonyms and scope wording
    across files.  Values never enter this payload; the engine remains the
    numeric authority.
    """
    facts = {f['id']: f for f in ws['facts']}
    documents = {d['id']: d for d in ws['documents']}
    grouped = defaultdict(list)
    for claim in ws['claims']:
        if not claim.get('confirmed') or claim.get('source') == 'ocr':
            continue
        refs = [facts[fid] for fid in claim.get('refs', []) if fid in facts]
        if not refs:
            continue
        for fact in refs:
            key = (str(fact.get('subject', '')).strip(), str(fact.get('period', '')).strip())
            grouped[key].append((claim, fact))
    result = []
    for index, (key, rows) in enumerate(grouped.items(), 1):
        file_ids = {claim.get('file_id') for claim, _ in rows} | {fact.get('file_id') for _, fact in rows}
        if len(file_ids) < 2:
            continue
        result.append({
            'group_id': f'semantic-{index}',
            'subject': key[0], 'period': key[1],
            'file_ids': sorted(i for i in file_ids if i),
            'claims': [{
                'claim_id': claim['id'],
                'file_id': claim.get('file_id'),
                'file_name': documents.get(claim.get('file_id'), {}).get('name', ''),
                'kind': claim.get('kind'),
                'text': _redact_text(claim.get('original', '')),
                'metric': fact.get('metric', ''),
                'period': fact.get('period', ''),
                'unit': fact.get('unit', ''),
                'scope': fact.get('scope', ''),
                'fact_id': fact.get('id'),
            } for claim, fact in rows]
        })
    return result


def _redact_text(value):
    # Keep this module usable offline; importing llm lazily avoids a module
    # cycle and uses the same conservative number redaction as link prompts.
    from .llm import redact_numbers
    return redact_numbers(str(value or ''))


def semantic_findings(ws, audit):
    """Ask the optional semantic audit agent for controlled risk proposals."""
    groups = audit.get('semantic_groups') or build_semantic_groups(ws)
    if not groups:
        return [], 0
    from . import llm
    proposals = llm.suggest_cross_document_findings(groups)
    return proposals, sum(len(group.get('claims', [])) for group in groups)


def add_semantic_findings(ws, audit, proposals):
    claims = {c['id']: c for c in ws['claims']}
    allowed_categories = {'semantic_scope_risk', 'semantic_version_conflict',
                          'semantic_contradiction', 'semantic_duplicate', 'semantic_unit_risk'}
    for proposal in proposals:
        if proposal.get('category') not in allowed_categories:
            continue
        ids = proposal.get('claim_ids') or []
        if not isinstance(ids, list) or not isinstance(proposal.get('reason'), str):
            continue
        if any(cid not in claims for cid in ids) or len({claims[cid]['file_id'] for cid in ids}) < 2:
            continue
        refs = list(dict.fromkeys(fid for cid in ids for fid in claims[cid]['refs']))
        if not refs or any(char.isdigit() for char in proposal['reason']):
            # The semantic model receives no values and must not invent any in
            # its explanation.  Numeric adjudication belongs to engine.check.
            continue
        duplicate = any(f.get('source') == 'model' and f.get('category') == proposal['category']
                        and f.get('claim_ids') == ids for f in audit['findings'])
        if not duplicate:
            audit['findings'].append(_finding(ws, proposal['category'], ids, refs,
                                             proposal['reason'][:500], source='model', numeric=False))
    return audit


def repair_plan(ws):
    documents = {d['id']: d for d in ws['documents']}
    audit = ws.get('cross_audit') or {}
    audit_current = bool(audit and audit.get('signature') == signature(ws) and not audit.get('stale'))
    findings = audit.get('findings', []) if audit_current else []
    blocked = (ws.get('fact_quality') or {}).get('auto_repair_allowed') is False
    items = []
    for c in ws['claims']:
        if not c['confirmed'] or c.get('source') == 'ocr' or c.get('repairable') is False:
            continue
        checked = engine.check(c, ws['facts'])
        if checked['status'] != 'inconsistent' or not checked.get('expected'):
            continue
        risks = []
        if c['kind'] == 'chart':
            risks.append('原生图表需重新读取 XML，并用 Office 渲染结果进行视觉复核')
        if any(c['id'] in f['claim_ids'] for f in findings):
            risks.append('涉及跨文档审计疑点，须逐项复核')
        if blocked:
            risks.append('事实源质量阻断，当前禁止写回')
        items.append({'claim_id': c['id'], 'file_id': c['file_id'],
                      'file_name': documents.get(c['file_id'], {}).get('name', ''),
                      'kind': c['kind'], 'label': c['label'], 'location': c['location'],
                      'start': c.get('start'), 'end': c.get('end'),
                      'before': c['original'], 'after': checked['expected'],
                      'refs': list(c['refs']), 'risk_level': 'high' if risks else 'normal',
                      'risk_reasons': risks, 'write_allowed': not blocked,
                      'influence': influence(ws, c['refs']),
                      'verification': ['原文锚点', '重新读取 Office', '修复项确定性核验', '无关正文保持一致']})
    return {'signature': signature(ws), 'source_revision': ws.get('revision'), 'items': items,
            'requires_approval': True,
            'model_status': 'not_requested', 'stale': False,
            'audit_stale': bool(audit and not audit_current),
            'summary': {'total': len(items), 'files': len({i['file_id'] for i in items}),
                        'high_risk': sum(i['risk_level'] == 'high' for i in items),
                        'blocked': sum(not i['write_allowed'] for i in items)}}
