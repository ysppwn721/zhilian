"""Sweep local BGE score/margin gates on the frozen semantic rewrite set.

This is an evaluation artifact only.  The dataset is programmatic and small;
the output is used to inspect trade-offs, not to claim a production threshold.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from zhilian.office import read_facts
from zhilian.reranker import score_pairs

DATA = ROOT / 'semantic_rewrite_eval_v2.jsonl'
FACTS = ROOT.parent / '长文Word测试/配套数据_初始.xlsx'
OUT = ROOT / 'semantic_threshold_calibration_v1'


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return center - half, center + half


def main() -> None:
    os.environ['ZHILIAN_LOCAL_RERANKER_ENABLED'] = '1'
    os.environ['ZHILIAN_LOCAL_RERANKER_PATH'] = str(ROOT.parent / 'models/bge-reranker-v2-m3-onnx-int8')
    os.environ['ZHILIAN_LOCAL_RERANKER_DEVICE'] = 'cpu'
    rows = [json.loads(line) for line in DATA.open(encoding='utf-8') if line.strip()]
    facts = read_facts(FACTS, 'facts')
    by_id = {f['id']: f for f in facts}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['claim_id']].append(row)
    scored_claims = []
    for claim_id, items in grouped.items():
        claim = items[0]['claim_text']
        scores = score_pairs(claim, facts, batch_size=16)
        gold = next(row['fact_id'] for row in items if row['label'] == 1)
        top = scores[0] if scores else {'fact_id': None, 'raw_score': -999.0}
        second = scores[1]['raw_score'] if len(scores) > 1 else -999.0
        scored_claims.append({'claim_id': claim_id, 'category': items[0]['category'],
                              'gold': gold, 'top_fact': top['fact_id'],
                              'raw_score': float(top['raw_score']),
                              'margin': float(top['raw_score']) - float(second)})
    thresholds = [-4.0, -3.5, -3.0, -2.5, -2.0, -1.5, -1.0]
    margins = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]
    output = []
    for threshold in thresholds:
        for margin in margins:
            accepted = [r for r in scored_claims if r['raw_score'] >= threshold and r['margin'] >= margin]
            correct = sum(r['top_fact'] == r['gold'] for r in accepted)
            wrong = len(accepted) - correct
            low, high = wilson(correct, len(accepted))
            output.append({
                'threshold': threshold, 'margin': margin, 'claims': len(scored_claims),
                'accepted': len(accepted), 'correct': correct, 'wrong': wrong,
                'coverage': round(len(accepted) / len(scored_claims), 6),
                'abstain_rate': round(1 - len(accepted) / len(scored_claims), 6),
                'precision_when_linked': round(correct / len(accepted), 6) if accepted else None,
                'error_association_rate': round(wrong / len(scored_claims), 6),
                'precision_wilson_low': round(low, 6), 'precision_wilson_high': round(high, 6),
                'label_basis': 'programmatic semantic rewrite; evaluation-only',
            })
    OUT.mkdir(exist_ok=True)
    with (OUT / 'thresholds.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output[0]))
        writer.writeheader(); writer.writerows(output)
    (OUT / 'raw_scores.json').write_text(json.dumps(scored_claims, ensure_ascii=False, indent=2), encoding='utf-8')
    chosen = next(item for item in output if item['threshold'] == -3.0 and item['margin'] == 0.1)
    report = {'dataset': str(DATA), 'claims': len(scored_claims), 'facts': len(facts),
              'chosen_gate_for_comparison': chosen,
              'warning': 'Use as a trade-off curve only; the programmatic set is not human gold and is too small for final threshold registration.',
              'metrics': output}
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'chosen_gate_for_comparison': chosen, 'rows': len(output)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
