"""Validate the completed v3 blind review and build a human-gold subset.

The reviewed IDs, rather than the programmatic benchmark labels, become the
gold source set. Abstained claims are kept in the audit output but excluded
from model scoring because no human source decision was made.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW = ROOT / "答辩评测/v3_eval_20261005/v3_review_sheet_40.csv"
DEFAULT_BENCHMARK = ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl"
DEFAULT_OUT = ROOT / "答辩评测/v3_eval_20261005/v3_human_gold_40"


def read_benchmark(path: Path) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        groups.setdefault(row["claim_id"], []).append(row)
    return groups


def split_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    ap.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    groups = read_benchmark(args.benchmark)
    with args.review.open(encoding="utf-8-sig", newline="") as stream:
        reviews = list(csv.DictReader(stream))

    audit = []
    gold_rows = []
    errors = []
    for review in reviews:
        claim_id = review["review_id"].strip()
        group = groups.get(claim_id)
        if not group:
            errors.append({"review_id": claim_id, "error": "unknown_claim_id"})
            continue
        candidate_ids = {row["fact_id"] for row in group}
        selected = split_ids(review.get("review_selected_fact_ids", ""))
        abstain = review.get("review_abstain", "").strip() == "1"
        expected_k = int(group[0]["expected_k"])
        row_errors = []
        if any(item not in candidate_ids for item in selected):
            row_errors.append("selected_fact_id_not_in_candidates")
        if not abstain and len(selected) != expected_k:
            row_errors.append(f"expected_{expected_k}_selected_ids")
        if abstain and selected:
            row_errors.append("abstain_with_selected_fact_ids")
        if not abstain and len(set(selected)) != len(selected):
            row_errors.append("duplicate_selected_fact_id")
        if row_errors:
            errors.append({"review_id": claim_id, "error": ";".join(row_errors)})

        audit.append({
            "review_id": claim_id,
            "task_type": group[0]["task_type"],
            "selected_fact_ids": selected,
            "abstain": abstain,
            "notes": review.get("review_notes", ""),
            "valid": not row_errors,
        })
        if row_errors or abstain:
            continue
        selected_set = set(selected)
        for row in group:
            output = dict(row)
            output["gold_fact_ids"] = selected
            output["label"] = int(row["fact_id"] in selected_set)
            output["human_reviewed"] = True
            output["programmatic_label"] = int(row.get("label", 0))
            gold_rows.append(output)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "review_audit.json").write_text(
        json.dumps({
            "review_rows": len(reviews),
            "valid_decisions": sum(item["valid"] and not item["abstain"] for item in audit),
            "abstained": sum(item["abstain"] for item in audit),
            "invalid": len(errors),
            "errors": errors,
            "decisions": audit,
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.output_dir / "human_gold.jsonl").open("w", encoding="utf-8") as stream:
        for row in gold_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "source_review": str(args.review),
        "source_benchmark": str(args.benchmark),
        "review_rows": len(reviews),
        "claims_scored": len({row["claim_id"] for row in gold_rows}),
        "candidate_rows_scored": len(gold_rows),
        "abstained_claims": sum(item["abstain"] for item in audit),
        "invalid_rows": len(errors),
        "labels": "human_reviewed_source_ids",
        "programmatic_labels_not_used_for_scoring": True,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
