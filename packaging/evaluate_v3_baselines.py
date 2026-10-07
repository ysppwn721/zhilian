"""Evaluate deterministic baselines on the real-value v3 benchmark."""
from __future__ import annotations

import json
import re
import argparse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl"
OUT = ROOT / "答辩评测/v3_eval_20261005/v3_baselines.json"
FACTORS = {"元": 1.0, "千元": 1e3, "万元": 1e4, "亿元": 1e8, "%": 1.0, "元/股": 1.0, "万TEU": 1.0}
ALIASES = {
    "营收": "营业收入", "销售收入": "营业收入", "营业总收入": "营业收入", "销售金额": "营业收入",
    "归母净利润": "归属于上市公司股东的净利润", "归母净资产": "归属于上市公司股东的净资产",
    "净利": "净利润", "销售开支": "销售费用", "管理开支": "管理费用", "研发开支": "研发费用",
}


def num(text: str) -> float | None:
    try:
        return float(str(text).replace(",", "").replace("−", "-").strip("()"))
    except (ValueError, TypeError):
        return None


def sentence_numbers(text: str) -> list[tuple[float, str]]:
    result = []
    for match in re.finditer(r"[-−(]?\d[\d,]*(?:\.\d+)?", text):
        value = num(match.group(0))
        if value is None:
            continue
        tail = text[match.end():match.end() + 8].strip()
        unit_match = re.match(r"(亿元|万元|千元|元/股|万TEU|元|%)", tail)
        result.append((value, unit_match.group(1) if unit_match else "元"))
    return result


def value_matches(row: dict, text: str) -> bool:
    value = num(row.get("fact_value"))
    if value is None:
        return False
    row_unit = row.get("unit") or "元"
    for shown, shown_unit in sentence_numbers(text):
        if row_unit not in FACTORS or shown_unit not in FACTORS:
            if abs(value - shown) <= max(1e-6, abs(shown) * 1e-6):
                return True
            continue
        converted = value * FACTORS[row_unit] / FACTORS[shown_unit]
        if abs(converted - shown) <= max(0.02, abs(shown) * 1e-6):
            return True
    return False


def metric_matches(row: dict, text: str) -> bool:
    metric = row.get("metric", "")
    if metric and metric in text:
        return True
    return any(alias in text and canonical == metric for alias, canonical in ALIASES.items())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATA)
    ap.add_argument("--output", type=Path, default=OUT)
    args = ap.parse_args()
    data = args.dataset
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in data.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            groups[row["claim_id"]].append(row)

    stats = {
        "dataset": str(data),
        "claims": len(groups),
        "programmatic_labels": not any(bool(row.get("human_reviewed")) for group in groups.values() for row in group),
        "human_gold": any(bool(row.get("human_reviewed")) for group in groups.values() for row in group),
        "baselines": {},
    }
    field_correct = numeric_correct = 0
    field_answered = numeric_answered = 0
    growth_field_exact = growth_numeric_exact = 0
    growth_numeric_precision = growth_numeric_recall = 0.0
    quote_n = growth_n = 0
    for group in groups.values():
        claim = group[0]["claim_text"]
        gold = {row["fact_id"] for row in group if row["label"]}
        if group[0]["task_type"] == "growth_set":
            growth_n += 1
            field = {row["fact_id"] for row in group if metric_matches(row, claim)}
            numeric = {row["fact_id"] for row in group if metric_matches(row, claim) and value_matches(row, claim)}
            growth_field_exact += int(field == gold)
            growth_numeric_exact += int(numeric == gold)
            growth_numeric_precision += len(numeric & gold) / max(1, len(numeric))
            growth_numeric_recall += len(numeric & gold) / max(1, len(gold))
        else:
            quote_n += 1
            field = [row for row in group if metric_matches(row, claim)]
            numeric = [row for row in field if value_matches(row, claim)]
            if len(field) == 1:
                field_answered += 1
                field_correct += int(field[0]["fact_id"] in gold)
            if len(numeric) == 1:
                numeric_answered += 1
                numeric_correct += int(numeric[0]["fact_id"] in gold)
    stats["baselines"]["metric_only"] = {
        "quote_claims": quote_n, "quote_answered": field_answered,
        "quote_coverage": field_answered / max(1, quote_n),
        "quote_correct_over_all": field_correct / max(1, quote_n),
        "growth_claims": growth_n, "growth_exact_match": growth_field_exact / max(1, growth_n),
    }
    stats["baselines"]["metric_plus_numeric_value_and_unit"] = {
        "quote_claims": quote_n, "quote_answered": numeric_answered,
        "quote_coverage": numeric_answered / max(1, quote_n),
        "quote_correct_over_all": numeric_correct / max(1, quote_n),
        "growth_claims": growth_n, "growth_exact_match": growth_numeric_exact / max(1, growth_n),
        "growth_precision": growth_numeric_precision / max(1, growth_n),
        "growth_recall": growth_numeric_recall / max(1, growth_n),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
