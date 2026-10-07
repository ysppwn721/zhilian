"""Conservative, read-only triage for merged annual-report candidates.

The output is a review queue, not a training set.  In particular, this tool
never turns an extracted row into a positive label.  It makes the unresolved
scope and arithmetic risks visible so the next audit pass can focus on the
smallest useful subset.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def triage(row: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    scope = str(row.get("scope") or "").strip()
    if scope in {"", "未标明", "unknown"}:
        reasons.append("scope_not_explicit")
    if row.get("unit") in (None, "", "unknown"):
        reasons.append("unit_missing")
    if row.get("three_value_check") in {"inconsistent", "change_looks_like_amount"}:
        reasons.append("three_value_check_failed")
    if row.get("match_level") == "fragment":
        reasons.append("weak_fragment_match")
    if not row.get("claim_text"):
        reasons.append("claim_missing")
    if "three_value_check_failed" in reasons or "claim_missing" in reasons:
        return "reject_or_abstain", reasons
    if "scope_not_explicit" in reasons or "unit_missing" in reasons or "weak_fragment_match" in reasons:
        return "needs_scope_unit_review", reasons
    return "evidence_complete_pending_label", reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        default=Path("答辩评测/annual_reports_merged/candidates_v2.jsonl"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("答辩评测/annual_reports_merged/auto_triage.jsonl"),
    )
    parser.add_argument(
        "--summary", type=Path,
        default=Path("答辩评测/annual_reports_merged/auto_triage_summary.json"),
    )
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    usable = [row for row in rows if row.get("claim_kind") in {"growth", "quote"}]
    output_rows = []
    status_counts = Counter()
    reason_counts = Counter()
    for row in usable:
        status, reasons = triage(row)
        item = {
            "company": row.get("company"),
            "source_file": row.get("source_file"),
            "claim_text": row.get("claim_text"),
            "claim_kind": row.get("claim_kind"),
            "metric": row.get("metric"),
            "period": row.get("period"),
            "unit": row.get("unit"),
            "scope": row.get("scope"),
            "claim_page": row.get("claim_page"),
            "fact_page": row.get("fact_page"),
            "three_value_check": row.get("three_value_check"),
            "match_level": row.get("match_level"),
            "negative_pool_size": row.get("negative_pool_size", 0),
            "label": None,
            "triage_status": status,
            "triage_reasons": reasons,
        }
        output_rows.append(item)
        status_counts[status] += 1
        reason_counts.update(reasons)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output_rows),
        encoding="utf-8",
    )
    summary = {
        "source": str(args.input),
        "candidate_rows": len(output_rows),
        "companies": len({row["company"] for row in output_rows}),
        "label_status": "all_pending; no labels assigned",
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts),
        "hard_negative_pool": sum(int(row.get("negative_pool_size") or 0) for row in usable),
        "training_ready": False,
        "blocking_reasons": [
            "label_scope_and_period_audit_not_approved",
            "source_set_labels_not_promoted",
            "company_disjoint_train_dev_test_not_built",
        ],
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
