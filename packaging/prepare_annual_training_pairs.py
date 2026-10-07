"""Prepare a company-isolated weak-label reranker dataset from annual reports.

The source contains programmatic positives and hard negatives.  This command
does not edit the source corpus or the frozen evaluation set.  It deliberately
marks the output as weakly supervised: the labels are suitable for a research
training run, not for claiming an audited gold benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def company_key(row: dict) -> str:
    return str(row.get("company") or "").strip()


def assign_splits(rows: list[dict]) -> dict[str, str]:
    counts = Counter(company_key(row) for row in rows)
    companies = sorted(counts, key=lambda code: (-counts[code], hashlib.sha256(code.encode()).hexdigest()))
    slots = ("test", "dev", "train")
    assignment: dict[str, str] = {}
    for index, company in enumerate(companies):
        assignment[company] = slots[index % len(slots)]
    return assignment


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.out.exists():
        raise SystemExit(f"refusing to overwrite existing directory: {args.out}")

    rows = load_rows(args.source / "negatives_v1.jsonl")
    source_row_count = len(rows)
    required = {"claim_id", "claim_text", "fact_text", "label", "company"}
    missing = required - set(rows[0]) if rows else required
    if missing:
        raise SystemExit(f"source is missing fields: {sorted(missing)}")
    if any(row["label"] not in (0, 1) for row in rows):
        raise SystemExit("source contains labels outside 0/1")

    # A ranking claim must have at least one positive and one negative.  Keep
    # the exclusion explicit; a positive-only claim cannot teach ranking.
    labels_by_claim: dict[str, set[int]] = {}
    for row in rows:
        labels_by_claim.setdefault(str(row["claim_id"]), set()).add(int(row["label"]))
    excluded_claims = sorted(
        claim_id for claim_id, labels in labels_by_claim.items() if labels != {0, 1}
    )
    rows = [row for row in rows if str(row["claim_id"]) not in set(excluded_claims)]

    split_by_company = assign_splits(rows)
    output_rows: dict[str, list[dict]] = {"train": [], "dev": [], "test": []}
    for row in rows:
        company = company_key(row)
        split = split_by_company[company]
        item = dict(row)
        item["group_id"] = f"annual-company-{company}"
        item["split"] = split
        item["label_source"] = "programmatic_weak_label"
        item["source_corpus"] = str(args.source)
        output_rows[split].append(item)

    group_splits: dict[str, set[str]] = {}
    for split, split_rows in output_rows.items():
        for row in split_rows:
            group_splits.setdefault(row["group_id"], set()).add(split)
    leakage = {group: values for group, values in group_splits.items() if len(values) != 1}
    if leakage:
        raise SystemExit(f"company leakage detected: {sorted(leakage)[:5]}")

    args.out.mkdir(parents=True)
    for split, split_rows in output_rows.items():
        with (args.out / f"{split}.jsonl").open("w", encoding="utf-8") as stream:
            for row in split_rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    period_by_split = {}
    for split, split_rows in output_rows.items():
        positives = [row for row in split_rows if row["label"] == 1]
        periods = Counter(
            (row.get("fact_text") or "").split("；")[2]
            for row in positives
            if "；" in (row.get("fact_text") or "")
        )
        period_by_split[split] = dict(periods)

    manifest = {
        "ready_for_training": True,
        "label_quality": "programmatic_weak_labels",
        "evaluation_warning": "Do not report this dataset as human-annotated gold; keep the frozen evaluation set separate.",
        "source": str(args.source),
        "source_rows": source_row_count,
        "excluded_claims_without_both_labels": excluded_claims,
        "company_disjoint": True,
        "splits": {
            split: {
                "rows": len(split_rows),
                "claims": len({row["claim_id"] for row in split_rows}),
                "companies": len({row["company"] for row in split_rows}),
                "positives": sum(row["label"] == 1 for row in split_rows),
                "negatives": sum(row["label"] == 0 for row in split_rows),
            }
            for split, split_rows in output_rows.items()
        },
        "positive_periods": period_by_split,
        "frozen_evaluation_reused": False,
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out / "README.md").write_text(
        "# Annual-report weak-label training set\n\n"
        "This is a research training set built from programmatic claim/fact links. "
        "It contains both current-period and prior-period positives, plus hard negatives. "
        "It is not a human-annotated gold benchmark. The frozen project evaluation set "
        "must remain separate.\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
