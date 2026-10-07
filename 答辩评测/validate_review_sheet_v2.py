"""Validate the human-review CSV before promoting rows into training data.

The sheet is deliberately treated as an input form.  Blank decisions remain
unapproved; this script reports readiness and never writes labels or changes
the source CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

DECISIONS = {"accept", "positive", "reject", "abstain", "review"}
SCOPES = {"合并", "母公司", "分部", "未标明", "unknown", ""}
UNITS = {"元", "万元", "亿元", "千元", "百万元", "unknown", ""}
REQUIRED = {
    "序号", "类型", "公司代码", "公司名", "指标(表格原文)", "本期值", "上期值",
    "单位", "口径", "正文页", "表页", "原句", "人工判定",
    "口径确认(合并/母公司/分部)", "单位确认(元/万元)",
}


def clean(value: object) -> str:
    return str(value or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("答辩评测/annual_reports_new_20261002/review_sheet_v2.csv"),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    with args.csv.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        rows = list(reader)

    missing_fields = sorted(REQUIRED - fields)
    issues: list[dict] = []
    for row in rows:
        index = clean(row.get("序号"))
        decision = clean(row.get("人工判定")).lower()
        scope = clean(row.get("口径确认(合并/母公司/分部)"))
        unit = clean(row.get("单位确认(元/万元)"))
        if not decision:
            issues.append({"row": index, "reason": "decision_blank"})
        elif decision not in DECISIONS:
            issues.append({"row": index, "reason": "decision_unknown", "value": decision})
        if scope not in SCOPES:
            issues.append({"row": index, "reason": "scope_unknown", "value": scope})
        if unit not in UNITS:
            issues.append({"row": index, "reason": "unit_unknown", "value": unit})
        if not clean(row.get("原句")):
            issues.append({"row": index, "reason": "claim_blank"})
        if not clean(row.get("正文页")):
            issues.append({"row": index, "reason": "claim_page_blank"})
        if not clean(row.get("表页")):
            issues.append({"row": index, "reason": "fact_page_blank"})

    decision_counts = Counter(clean(row.get("人工判定")).lower() or "blank" for row in rows)
    scope_counts = Counter(clean(row.get("口径确认(合并/母公司/分部)")) or "blank" for row in rows)
    unit_counts = Counter(clean(row.get("单位确认(元/万元)")) or "blank" for row in rows)
    type_counts = Counter(clean(row.get("类型")) for row in rows)
    company_count = len({clean(row.get("公司代码")) for row in rows if clean(row.get("公司代码"))})
    hard_negatives = sum(int(float(clean(row.get("难负例数")) or 0)) for row in rows)
    accepted = sum(decision_counts.get(value, 0) for value in ("accept", "positive"))
    rejected = decision_counts.get("reject", 0)
    abstain = decision_counts.get("abstain", 0)
    report = {
        "source": str(args.csv),
        "rows": len(rows),
        "companies": company_count,
        "missing_fields": missing_fields,
        "type_counts": dict(type_counts),
        "decision_counts": dict(decision_counts),
        "scope_counts": dict(scope_counts),
        "unit_counts": dict(unit_counts),
        "hard_negative_slots": hard_negatives,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
        "abstain_rows": abstain,
        "ready_for_label_promotion": not missing_fields and not issues,
        "issues": issues,
        "promotion_rule": "Only accepted rows with confirmed unit and scope may be promoted; growth rows require a two-fact source set.",
    }
    output = args.out or args.csv.with_name("review_sheet_v2_validation.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_label_promotion"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
