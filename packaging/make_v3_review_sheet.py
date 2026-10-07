"""Create a blind 40-claim v3 review sheet.

Gold labels are intentionally omitted from the CSV given to reviewers. The
source JSONL remains unchanged and can be used later to reconcile annotations.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl"
OUT = ROOT / "答辩评测/v3_eval_20261005/v3_review_sheet_40.csv"
README = ROOT / "答辩评测/v3_eval_20261005/v3_review_sheet_40.md"


def main() -> int:
    groups = defaultdict(list)
    for line in DATA.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            groups[row["claim_id"]].append(row)
    by_task = defaultdict(list)
    for group in groups.values():
        by_task[group[0]["task_type"]].append(group)
    # Deterministic, company-diverse selection: 20 single-value + 20 growth.
    chosen = []
    for task, count in (("quote_current", 20), ("growth_set", 20)):
        selected = []
        seen_companies = set()
        ordered = sorted(by_task[task], key=lambda g: (g[0]["company"], g[0]["claim_id"]))
        for group in ordered:
            company = group[0]["company"]
            if company not in seen_companies:
                selected.append(group)
                seen_companies.add(company)
            if len(selected) >= count:
                break
        if len(selected) < count:
            selected.extend(ordered[len(selected):count])
        chosen.extend(selected[:count])
    rows = []
    for group in chosen:
        head = group[0]
        candidates = "\n".join(
            f"{i + 1}. {row['fact_id']} | {row['fact_text']} | value={row.get('fact_value', '')}"
            for i, row in enumerate(sorted(group, key=lambda r: r["candidate_position"]))
        )
        rows.append({
            "review_id": head["claim_id"], "company": head["company"], "source_file": head["source_file"],
            "source_page": head.get("source_page", ""), "layer": head["layer"], "task_type": head["task_type"],
            "claim_text": head["claim_text"], "candidates": candidates,
            "review_selected_fact_ids": "", "review_abstain": "", "review_notes": "",
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    README.write_text(
        "# v3 人工复核表（40 条）\n\n"
        "本表从 v3 真实年报基准中抽取 20 条本期真实数值单值题和 20 条增长双来源题，来自不同公司。\n\n"
        "请在 `review_selected_fact_ids` 填写正确事实 ID；增长题填写本期与上期两个 ID，用分号分隔；无法确认时将 `review_abstain` 填为 1，并在备注中写原因。\n\n"
        "候选文本来自 PDF 抽取，人工复核应同时检查来源页和原句。Gold 标签没有写入本表，避免评测泄漏。\n",
        encoding="utf-8",
    )
    print(f"written: {OUT} ({len(rows)} claims)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
