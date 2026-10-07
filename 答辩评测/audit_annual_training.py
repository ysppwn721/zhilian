"""Audit annual-report training candidates without loading model weights."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from build_annual_report_text_pairs import company_key


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).parent
    parser.add_argument("--dataset", type=Path, default=root / "annual_report_text_pairs_v1")
    parser.add_argument("--frozen", type=Path, default=root / "external_programmatic_holdout_all.jsonl")
    args = parser.parse_args()
    splits = {name: load(args.dataset / f"{name}.jsonl") for name in ("train", "dev", "test")}
    companies = {name: {company_key(row['group_id']) for row in rows} for name, rows in splits.items()}
    overlap = {f"{a}/{b}": sorted(companies[a] & companies[b]) for a, b in
               (("train", "dev"), ("train", "test"), ("dev", "test"))}
    frozen = load(args.frozen)
    frozen_files = {row['source_file'] for row in frozen}
    frozen_companies = {company_key(row['group_id']) for row in frozen}
    train_files = {row['source_file'] for row in splits['train']}
    split_summary = {}
    for name, rows in splits.items():
        by_claim = defaultdict(list)
        for row in rows:
            by_claim[row['claim_id']].append(row)
        selections = [next((row for row in candidates if row['fact_id'].endswith('-current')), candidates[0])
                      for candidates in by_claim.values()]
        split_summary[name] = {
            'pairs': len(rows),
            'claims': len(by_claim),
            'companies': len(companies[name]),
            'original_claims': len({row['claim_id'] for row in rows if not row.get('semantic_variant')}),
            'generated_rewrite_pairs': sum(bool(row.get('semantic_variant')) for row in rows),
            'multi_positive_claims': sum(sum(row['label'] == 1 for row in candidates) > 1
                                         for candidates in by_claim.values()),
            'always_current_top1': sum(row['label'] == 1 for row in selections) / len(selections)
                                   if selections else None,
        }
    reasons = []
    if any(overlap.values()):
        reasons.append('company_leakage_between_splits')
    reused = sorted(train_files & frozen_files)
    if companies['train'] & frozen_companies:
        reasons.append('frozen_evaluation_source_reused_for_training')
    if split_summary['test']['always_current_top1'] == 1:
        reasons.append('current_period_selection_shortcut_scores_100_percent')
    manifest_path = args.dataset / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    if manifest.get('ready_for_training') is not True:
        reasons.append('label_scope_and_period_audit_not_approved')
    result = {
        'ready_for_training': not reasons,
        'blocking_reasons': reasons,
        'splits': split_summary,
        'company_overlap': overlap,
        'reused_frozen_train_reports': reused,
        'metric_scope': 'single-candidate Top-1 is insufficient to validate multi-fact source linking',
    }
    (args.dataset / 'quality_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 2 if reasons else 0


if __name__ == '__main__':
    raise SystemExit(main())
