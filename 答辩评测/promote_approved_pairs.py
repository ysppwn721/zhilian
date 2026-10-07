"""Promote an audited candidate dataset into the reranker training format.

This is a deliberate gate between review output and training.  It refuses
pending labels, incomplete candidate sets, group leakage, and the trivial
"always choose current period" construction.  It never edits the source
files; on failure it writes no output dataset.

Input directory:
  train.jsonl, dev.jsonl, test.jsonl, manifest.json

The source manifest must contain ``ready_for_training: true``.  Rows use the
same schema as ``train_reranker.py`` and may carry extra audit metadata.
Labels may be integers 0/1 or the review vocabulary ``positive`` and one of
the negative labels.  ``abstain`` is excluded rather than silently treated as
negative.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


NEGATIVE_LABELS = {
    "negative",
    "negative_wrong_metric",
    "negative_wrong_period",
    "negative_wrong_scope",
    "negative_wrong_subject",
    "hard_negative",
    "hard_negative_wrong_metric",
    "hard_negative_wrong_period",
    "hard_negative_wrong_scope",
}


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise ValueError(f"缺少文件: {path}")
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} JSON 无效: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_no} 不是对象")
        rows.append(row)
    return rows


def normalize_label(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, float) and value in (0.0, 1.0):
        return int(value)
    text = str(value or "").strip().lower()
    if text in {"1", "positive", "pos", "正例"}:
        return 1
    if text in {"0", *NEGATIVE_LABELS, "负例"}:
        return 0
    return None


def validate_split(name: str, rows: list[dict]) -> tuple[list[dict], list[str]]:
    required = {"claim_id", "claim_text", "fact_text", "group_id"}
    errors: list[str] = []
    normalized: list[dict] = []
    for index, row in enumerate(rows, 1):
        missing = sorted(key for key in required if not str(row.get(key) or "").strip())
        if missing:
            errors.append(f"{name}:{index} 缺少字段 {missing}")
            continue
        label = normalize_label(row.get("label"))
        if label is None:
            errors.append(f"{name}:{index} label 未确认或不是 0/1: {row.get('label')!r}")
            continue
        item = dict(row)
        item["label"] = label
        item["split"] = name
        normalized.append(item)

    by_claim: dict[str, list[dict]] = defaultdict(list)
    for row in normalized:
        by_claim[str(row["claim_id"])].append(row)
    for claim_id, candidates in by_claim.items():
        labels = {row["label"] for row in candidates}
        if labels != {0, 1}:
            errors.append(
                f"{name}:{claim_id} 候选集必须同时有正例和负例，实际标签={sorted(labels)}"
            )
    return normalized, errors


def audit(rows_by_split: dict[str, list[dict]], manifest: dict) -> list[str]:
    errors: list[str] = []
    if manifest.get("ready_for_training") is not True:
        errors.append("source_manifest_not_approved")
    groups: dict[str, set[str]] = defaultdict(set)
    for split, rows in rows_by_split.items():
        for row in rows:
            groups[str(row["group_id"])].add(split)
    overlap = {group: sorted(splits) for group, splits in groups.items() if len(splits) > 1}
    if overlap:
        errors.append(f"company_or_group_leakage:{len(overlap)}")

    # A valid multi-candidate test set must include both current and prior
    # positives somewhere; otherwise a period shortcut can look perfect.
    test = rows_by_split["test"]
    test_positive_periods = {
        str(row.get("period") or "")
        for row in test
        if row["label"] == 1
    }
    if test and len(test_positive_periods) < 2:
        errors.append("test_positive_period_balance_insufficient")

    if not all(rows_by_split[name] for name in ("train", "dev", "test")):
        errors.append("empty_train_dev_or_test")
    return errors


def write_dataset(out: Path, rows_by_split: dict[str, list[dict]], source: Path) -> None:
    out.mkdir(parents=True, exist_ok=False)
    for split, rows in rows_by_split.items():
        with (out / f"{split}.jsonl").open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "ready_for_training": True,
        "source": str(source),
        "label_vocabulary": {"positive": 1, "negative": 0, "abstain": "excluded"},
        "splits": {
            split: {
                "rows": len(rows),
                "claims": len({row["claim_id"] for row in rows}),
                "groups": len({row["group_id"] for row in rows}),
                "positives": sum(row["label"] == 1 for row in rows),
                "negatives": sum(row["label"] == 0 for row in rows),
            }
            for split, rows in rows_by_split.items()
        },
        "training_gate": "passed_normalization_and_split_audit",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"拒绝覆盖已有目录: {args.out}")
    manifest_path = args.source / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"缺少 manifest.json: {args.source}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    rows_by_split: dict[str, list[dict]] = {}
    errors: list[str] = []
    for split in ("train", "dev", "test"):
        normalized, split_errors = validate_split(
            split, load_jsonl(args.source / f"{split}.jsonl")
        )
        rows_by_split[split] = normalized
        errors.extend(split_errors)
    if not errors:
        errors.extend(audit(rows_by_split, manifest))
    if errors:
        print(json.dumps({"ready_for_training": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    write_dataset(args.out, rows_by_split, args.source)
    print(json.dumps({"ready_for_training": True, "output": str(args.out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
