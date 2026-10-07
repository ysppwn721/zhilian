"""Prepare an auditable staging set from the independent annual-report audit.

This command never assigns labels and never produces a trainable manifest.  It
keeps only records with a real narrative anchor so table-only rows cannot be
silently mistaken for claim/fact pairs.  The resulting files are intended for
review and later labeling.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

FROZEN = {
    "000615", "000930", "002097", "002388", "002413", "002425", "002569",
    "002598", "002808", "002825", "300149", "300632", "600080", "600165",
    "600187", "603839", "688152", "836263", "873576",
}
GROWTH_WORDS = ("同比", "环比", "增长", "增加", "减少", "下降", "上升", "增幅", "增减", "变动")
UNIT_WORDS = ("元", "万元", "亿元", "千元", "百万元")


def company_of(row: dict) -> str:
    value = str(row.get("company") or row.get("stock_code") or "")
    match = re.search(r"\d{6}", value)
    return match.group(0) if match else value


def split_for(company: str) -> str:
    bucket = int(hashlib.sha256(company.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 70 else ("dev" if bucket < 85 else "test")


def classify_anchor(row: dict) -> str:
    text = str(row.get("claim_text") or "")
    if any(word in text for word in GROWTH_WORDS):
        return "growth_sentence"
    kind = str(row.get("anchor_kind") or "")
    if "prior" in kind or row.get("period") in ("上期", "prior"):
        return "prior_anchor"
    return "current_anchor"


def explicit_unit(row: dict) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ("claim_text", "unit", "table_caption"))
    return any(unit in text for unit in UNIT_WORDS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path,
                        default=Path("答辩评测/annual_reports_new_20261002/audit_candidates.jsonl"))
    parser.add_argument("--out", type=Path,
                        default=Path("答辩评测/annual_report_training_staging_20261003"))
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.audit.read_text(encoding="utf-8").splitlines() if line.strip()]

    staging = []
    reasons = Counter()
    for row in rows:
        company = company_of(row)
        text = str(row.get("claim_text") or "").strip()
        status = str(row.get("audit_status") or "")
        row_reasons = list(row.get("audit_reasons") or [])
        if company in FROZEN:
            reasons["frozen_company"] += 1
            continue
        if not text or status == "rejected" or row.get("anchor_kind") == "table_only":
            reasons["missing_or_table_only_claim"] += 1
            continue
        record = dict(row)
        record["company"] = company
        record["anchor_type"] = classify_anchor(record)
        record["label"] = None
        record["label_status"] = "pending_human_scope_period_unit_review"
        record["split"] = split_for(company)
        record["source_is_real_pdf_text"] = True
        record["unit_explicit_in_available_text"] = explicit_unit(record)
        # Keep the reranker input aligned with production: facts contribute
        # metadata only; source numeric values stay in the audit record.
        record["fact_text"] = (
            f"subject={record.get('company_name') or company}；"
            f"metric={record.get('metric') or ''}；"
            f"period={record.get('period') or ''}；"
            f"unit={record.get('unit') or 'unknown'}；"
            f"scope={record.get('scope') or 'unknown'}"
        )
        record["candidate_label_options"] = [
            "positive", "negative_wrong_metric", "negative_wrong_period",
            "negative_wrong_scope", "abstain",
        ]
        record["audit_reasons"] = row_reasons
        staging.append(record)

    args.out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "dev", "test"):
        path = args.out / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for row in staging:
                if row["split"] == split:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_split = {
        split: {
            "rows": sum(row["split"] == split for row in staging),
            "companies": len({row["company"] for row in staging if row["split"] == split}),
            "anchors": dict(Counter(row["anchor_type"] for row in staging if row["split"] == split)),
        }
        for split in ("train", "dev", "test")
    }
    manifest = {
        "ready_for_training": False,
        "validation_status": "pending_human_labels_scope_period_unit_and_balance",
        "source_audit": str(args.audit),
        "source_rows": len(rows),
        "staging_rows_with_narrative_anchor": len(staging),
        "label_values": {"all": None},
        "company_disjoint": True,
        "splits": by_split,
        "rejection_summary": dict(reasons),
        "blocking_reasons": [
            "label_scope_and_period_audit_not_approved",
            "current_period_selection_shortcut_scores_100_percent",
            "prior_anchor_balance_insufficient",
            "unit_and_scope_evidence_incomplete",
        ],
        "training_rule": "Do not pass this directory to train_reranker.py until ready_for_training=true and labels are audited.",
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "labeling_template.md").write_text(
        "# 年报候选标注模板\n\n"
        "每条记录需要确认：主体、指标等价关系、单位、期间、合并/母公司/分部口径，以及候选是否为正确来源。"
        "label 只能填写 positive、negative_wrong_metric、negative_wrong_period、negative_wrong_scope 或 abstain。\n"
        "没有足够证据时使用 abstain，不要猜测。\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
