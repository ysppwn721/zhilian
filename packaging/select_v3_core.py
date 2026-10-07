"""Select a deterministic, auditable v3 core without inventing prior-only quotes.

The source pool has 45 valid real-value single-value claims and many growth
claims, but no valid prior-only single-value claims.  The core therefore
balances task families (45 quote_current + 45 growth_set) and labels that
scope explicitly instead of pretending period balance exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("答辩评测/v3_real_value_20261005/benchmark_v3.jsonl"))
    ap.add_argument("--output-dir", type=Path, default=Path("答辩评测/v3_real_value_20261005"))
    ap.add_argument("--quotes", type=int, default=45)
    ap.add_argument("--growth", type=int, default=45)
    args = ap.parse_args()

    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["claim_id"]].append(row)

    by_task: dict[str, list[list[dict]]] = defaultdict(list)
    for group in grouped.values():
        by_task[group[0]["task_type"]].append(group)

    def round_robin(groups: list[list[dict]], count: int) -> list[list[dict]]:
        by_company: dict[str, list[list[dict]]] = defaultdict(list)
        for group in sorted(groups, key=lambda g: g[0]["claim_id"]):
            by_company[group[0]["company"]].append(group)
        companies = sorted(by_company)
        selected: list[list[dict]] = []
        cursor = 0
        while len(selected) < count and companies:
            company = companies[cursor % len(companies)]
            if by_company[company]:
                selected.append(by_company[company].pop(0))
            companies = [name for name in companies if by_company[name]]
            cursor += 1
        if len(selected) < count:
            raise SystemExit(f"not enough claims: requested {count}, selected {len(selected)}")
        return selected

    quote_groups = round_robin(by_task.get("quote_current", []), args.quotes)
    growth_groups = round_robin(by_task.get("growth_set", []), args.growth)
    selected = quote_groups + growth_groups
    selected_rows = [row for group in selected for row in group]

    # Integrity checks are intentionally strict because this is an evaluation set.
    for group in selected:
        gold = {row["fact_id"] for row in group if row["label"]}
        if group[0]["task_type"] == "growth_set" and len(gold) != 2:
            raise SystemExit(f"growth claim does not have two distinct gold ids: {group[0]['claim_id']}")
        if len({row["fact_id"] for row in group}) != len(group):
            raise SystemExit(f"duplicate candidate id: {group[0]['claim_id']}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "benchmark_v3_core90.jsonl"
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in selected_rows) + "\n", encoding="utf-8")

    claims = len(selected)
    companies = len({row["company"] for row in selected_rows})
    first = sum(int(sorted(group, key=lambda r: r["candidate_position"])[0]["label"]) for group in selected) / claims
    last = sum(int(sorted(group, key=lambda r: r["candidate_position"])[-1]["label"]) for group in selected) / claims
    random_expected = sum(
        1 / len(group) if group[0]["task_type"] != "growth_set"
        else 1 / (len(group) * (len(group) - 1) / 2)
        for group in selected
    ) / claims
    real_by_task = {}
    for task in ("quote_current", "quote_prior", "growth_set"):
        task_groups = [group for group in selected if group[0]["task_type"] == task]
        real_by_task[task] = {
            "claims": len(task_groups),
            "claim_contains_real_value": sum(
                bool(re.search(r"\d[\d,.]*", group[0]["claim_text"]))
                for group in task_groups
            ) / max(1, len(task_groups)),
        }
    report = {
        "dataset": str(out),
        "source": str(args.input),
        "source_sha256": sha256(args.input),
        "sha256": sha256(out),
        "claims": claims,
        "rows": len(selected_rows),
        "companies": companies,
        "task_types": {"quote_current": len(quote_groups), "growth_set": len(growth_groups)},
        "period_balance": {"quote_current": len(quote_groups), "quote_prior": 0, "note": "池内没有合法的真实上期纯单值句，未伪造配平样本"},
        "claim_contains_real_value": sum(
            bool(re.search(r"\d[\d,.]*", group[0]["claim_text"]))
            for group in selected
        ) / claims,
        "claim_contains_real_value_by_task": real_by_task,
        "zero_information_baseline": {"first": first, "last": last, "random_expected": random_expected},
        "selection": "company round-robin; lexicographic claim order within company",
        "note": "任务平衡核心集：45 条真实数值单值 + 45 条真实增长双来源；不是本期/上期期间配平集。",
    }
    (args.output_dir / "benchmark_v3_core90_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
