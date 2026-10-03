"""把自动口径判定（scope_decisions.jsonl）落成可训练候选。

与 apply_adjudication.py 的区别：
  apply_adjudication.py 走人工台账(CSV)；
  本脚本走自动判定结果(JSONL)，用于 resolve_scope.py 的产出。

准入规则（任一条不满足即不进训练集）
  1. scope_verdict 非空（口径已判定）
  2. caption_coherent 为真（表头确实来自这条事实所在的表）
  3. three_value_check ∈ {consistent, no_change_value, sign_convention_differs}
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
ADMIT_CHECK = {'consistent', 'no_change_value', 'sign_convention_differs'}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    args = ap.parse_args()
    d = args.corpus

    dec = [json.loads(l) for l in (d / 'scope_decisions.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    overrides_path = d / 'scope_manual_overrides.json'
    overrides = json.loads(overrides_path.read_text(encoding='utf-8')) if overrides_path.is_file() else {}
    cands = {f"{c['company']}|{c['metric']}|{c['fact_page']}|{c['claim_kind']}": c
             for c in (json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip())
             if c['claim_kind'] in ('growth', 'quote')}

    admitted, pending, rejected = [], [], []
    for r in dec:
        key = f"{r['company']}|{r['metric']}|{r['fact_page']}|{r['claim_kind']}"
        rec = dict(cands.get(key, {}))
        manual = overrides.get(key, {})
        rec.update({
            'scope_confirmed': manual.get('scope_verdict', r.get('scope_verdict')),
            'scope_evidence_tier': manual.get('scope_evidence_tier', r.get('scope_evidence_tier')),
            'scope_reason': manual.get('scope_reason', r.get('scope_reason')),
            'scope_basis': manual.get('scope_basis', r.get('scope_basis')),
            'caption_coherent': r.get('caption_coherent'),
            'label': None,
        })
        if manual:
            rec['manual_override'] = True
            if manual.get('manual_note'):
                rec['manual_note'] = manual['manual_note']
            if manual.get('manual_reason'):
                rec['manual_reason'] = manual['manual_reason']
        if manual.get('manual_disposition') == 'reject':
            rec['admission'] = f"rejected_manual_{manual.get('manual_reason', 'manual_review')}"
            rec['manual_reason'] = manual.get('manual_reason')
            rejected.append(rec)
        elif not r.get('caption_coherent'):
            rec['admission'] = 'rejected_caption_table_mismatch'
            rejected.append(rec)
        elif not rec.get('scope_confirmed'):
            rec['admission'] = 'needs_manual_scope'
            pending.append(rec)
        elif r.get('three_value_check') not in ADMIT_CHECK:
            rec['admission'] = f"rejected_three_value_{r.get('three_value_check')}"
            rejected.append(rec)
        else:
            rec['admission'] = 'admitted'
            admitted.append(rec)

    (d / 'training_candidates.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in admitted), encoding='utf-8')
    (d / 'training_pending.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in pending), encoding='utf-8')
    (d / 'training_rejected.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rejected), encoding='utf-8')

    final_decisions = []
    for r in dec:
        key = f"{r['company']}|{r['metric']}|{r['fact_page']}|{r['claim_kind']}"
        manual = overrides.get(key, {})
        if manual:
            r = dict(r)
            for field in ('scope_verdict', 'scope_evidence_tier', 'scope_reason', 'scope_basis'):
                if field in manual:
                    r[field] = manual[field]
            if manual.get('manual_disposition'):
                r['manual_disposition'] = manual['manual_disposition']
            if manual.get('manual_reason'):
                r['manual_reason'] = manual['manual_reason']
            if manual.get('manual_note'):
                r['manual_note'] = manual['manual_note']
        final_decisions.append(r)
    (d / 'scope_decisions_final.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in final_decisions), encoding='utf-8')

    final_summary = {
        'total_scope_decisions': len(dec),
        'manual_override_records': len(overrides),
        'training_candidates': len(admitted),
        'training_pending': len(pending),
        'training_rejected': len(rejected),
        'candidate_scope_counts': dict(Counter(r.get('scope_confirmed') for r in admitted)),
        'candidate_tier_counts': dict(Counter(r.get('scope_evidence_tier') for r in admitted)),
        'candidate_claim_kind_counts': dict(Counter(r.get('claim_kind') for r in admitted)),
        'rejection_admission_counts': dict(Counter(r.get('admission') for r in rejected)),
        'manual_decision_note': '自动判定结果保留在 scope_decisions.jsonl；本汇总叠加 scope_manual_overrides.json 后生成。',
    }
    (d / 'scope_decisions_final_summary.json').write_text(
        json.dumps(final_summary, ensure_ascii=False, indent=2), encoding='utf-8')

    print('=== 准入结果 ===')
    print(f'  可进入训练 : {len(admitted)}')
    print(f'  待人工口径 : {len(pending)}')
    print(f'  已拒答     : {len(rejected)}')
    print()
    if admitted:
        print(f'  口径 : {dict(Counter(r["scope_confirmed"] for r in admitted))}')
        print(f'  依据 : {dict(Counter(r["scope_basis"] or "T3_structural" for r in admitted))}')
        print(f'  类型 : {dict(Counter(r["claim_kind"] for r in admitted))}')
        print(f'  单位 : {dict(Counter(r.get("unit") or "空" for r in admitted))}')
        print(f'  公司 : {len({r["company"] for r in admitted})} 家')
        print(f'  本期:上期来源 = {len(admitted)}:{sum(1 for r in admitted if r["claim_kind"] == "growth")}')
        print()
        print(f'  口径依据分档: T1 显式 {sum(1 for r in admitted if r["scope_evidence_tier"]=="T1")} · '
              f'T2 数值交叉 {sum(1 for r in admitted if r["scope_evidence_tier"]=="T2")} · '
              f'T3 准则/结构 {sum(1 for r in admitted if r["scope_evidence_tier"]=="T3")}')
    print()
    print(f'  → training_candidates.jsonl / training_pending.jsonl / training_rejected.jsonl')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
