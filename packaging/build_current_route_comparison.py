"""Compare current local models and an offline three-layer annual-report route.

The route is evaluated on the same 37 human-Gold claims. Rules select only a
unique candidate that passes metric/period/value evidence; ambiguous claims go
to the annual-report BERT. No API claim is made because same-set API calls are
not available in this environment.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "答辩评测"
OUT = EVAL / "v3_eval_20261005" / "v3_human_gold_40"
sys.path.insert(0, str(EVAL))
import evaluate_repaired_annual_benchmark as evaluator  # noqa: E402

NUM_RE = re.compile(r"(?<![\d.])-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")
UNIT_RE = re.compile(r"亿元|万元|元/股|元|%|件|人")
UNITS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000"), "%": Decimal("1"), "元/股": Decimal("1")}
PERIODS = {"本期": ("本期", "本报告期", "报告期内", "报告期", "本年度", "本年", "当期", "今年"),
           "上期": ("上期", "上年度同期", "上年同期", "去年同期", "上年度", "上年", "去年")}


def num(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def evidence(row):
    text = str(row.get("claim_text") or "")
    metric = str(row.get("metric") or "")
    alias = evaluator.CONTROLLED_ALIASES.get(metric, metric)
    names = {item for item in (metric, alias) if item}
    names |= {re.sub(r"[（(].*?[）)]", "", item) for item in names}
    metric_ok = any(name in text for name in names if name)
    period = str(row.get("period") or "")
    period_ok = any(token in text for token in PERIODS.get(period, (period,)))
    unit = str(row.get("unit") or "")
    unit_ok = bool(unit and unit in text)
    value_ok = False
    value = row.get("fact_value")
    if value is not None and unit in UNITS:
        target = num(value) * UNITS[unit]
        for match in NUM_RE.finditer(text):
            tail = text[match.end():match.end() + 5]
            unit_match = UNIT_RE.match(tail)
            if not unit_match or unit_match.group() not in UNITS:
                continue
            observed = num(match.group()) * UNITS[unit_match.group()]
            if abs(observed - target) <= max(Decimal("0.02"), abs(target) * Decimal("0.000002")):
                value_ok = True
                break
    return metric_ok, period_ok, unit_ok, value_ok


def unique_rule(group):
    task = group[0]["task_type"]
    if task == "growth_set":
        matches = [row for row in group if evidence(row)[0] and evidence(row)[3]]
        by_period = {row["period"]: row for row in matches}
        if set(by_period) >= {"本期", "上期"} and len(by_period) == 2:
            return [by_period["本期"]["fact_id"], by_period["上期"]["fact_id"]]
        return None
    matches = [row for row in group if all(evidence(row)[i] for i in (0, 1, 3))]
    return [matches[0]["fact_id"]] if len(matches) == 1 else None


def metrics(rows, predictions):
    return evaluator.evaluate(rows, {(r["claim_id"], r["fact_id"]): score for r, score in predictions.items()})


def prediction_map(rows, scores):
    return {(row["claim_id"], row["fact_id"]): score for row, score in zip(rows, scores)}


def score_to_predictions(rows, score_map, route_sources):
    groups = evaluator.group_rows(rows)
    out = []
    for cid, group in groups.items():
        rule = route_sources[cid]
        if rule["source"] == "rules":
            chosen = set(rule["refs"])
        else:
            ordered = sorted(group, key=lambda row: (-score_map[(cid, row["fact_id"])], row["fact_id"]))
            chosen = {row["fact_id"] for row in ordered[: int(group[0]["expected_k"])]}
        for row in group:
            # score map is used by evaluator.rank; encode route choice as a
            # dominating score while retaining deterministic tie-breaks.
            value = 2.0 if row["fact_id"] in chosen else 0.0
            out.append((row, value))
    return {(row["claim_id"], row["fact_id"]): value for row, value in out}


def flatten_metrics(raw):
    quote = raw["by_task"].get("quote_current", {})
    growth = raw["by_task"].get("growth_set", {})
    return {"quote_top1": quote.get("top1_accuracy", 0.0),
            "growth_exact": growth.get("source_set_exact_match", 0.0),
            "growth_precision": growth.get("source_set_precision", 0.0),
            "growth_recall": growth.get("source_set_completeness_recall", 0.0)}


def main() -> int:
    path = OUT / "human_gold.jsonl"
    rows = evaluator.read_rows(path)
    started = time.perf_counter()
    bge_scores = evaluator.score_rows(rows, "bge", 16, "cpu")
    bge_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    annual_full_scores = evaluator.score_rows(rows, "annual_repaired_v1", 32, "cpu")
    annual_full_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    # The routed path only scores claims that were not resolved uniquely by
    # rules. This is the actual local workload for the route, rather than a
    # full-dataset annual-model benchmark.
    all_groups = evaluator.group_rows(rows)
    rule_sources = {}
    for cid, group in all_groups.items():
        refs = unique_rule(group)
        rule_sources[cid] = {"source": "rules" if refs else "annual_bert", "refs": refs or []}
    annual_ids = {cid for cid, item in rule_sources.items() if item["source"] == "annual_bert"}
    annual_rows = [row for row in rows if row["claim_id"] in annual_ids]
    annual_scores = evaluator.score_rows(annual_rows, "annual_repaired_v1", 32, "cpu")
    annual_route_elapsed = time.perf_counter() - started
    bge_map = prediction_map(rows, bge_scores)
    annual_map = prediction_map(rows, annual_full_scores)
    annual_route_map = prediction_map(annual_rows, annual_scores)
    # For rule-resolved claims, annual_map is intentionally absent; route
    # evaluation uses only the rule refs for those groups.
    annual_map = {key: value for key, value in annual_map.items()}
    annual_route_map = {key: value for key, value in annual_route_map.items()}
    for row in rows:
        annual_route_map.setdefault((row["claim_id"], row["fact_id"]), -999.0)
    route_map = score_to_predictions(rows, annual_route_map, rule_sources)
    bge_raw = flatten_metrics(evaluator.evaluate(rows, bge_map))
    annual = flatten_metrics(evaluator.evaluate(rows, annual_map))
    routed = flatten_metrics(evaluator.evaluate(rows, route_map))
    counts = {}
    for item in rule_sources.values():
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    result = {
        "dataset": str(path), "claims_scored": len(evaluator.group_rows(rows)), "human_gold": True,
        "document_profile": {"profile": "annual", "selection": "automatic annual reranker", "basis": "normalized listed-company PDF name + report/audit text"},
        "models": {
            "bge": {**bge_raw, "elapsed_seconds": round(bge_elapsed, 3)},
            "bge_evidence_experiment": json.loads((OUT / "bge_evidence_rerank_experiment.json").read_text(encoding="utf-8"))["heldout_human_gold"],
            "annual_bert_auto_profile": {**annual, "elapsed_seconds": round(annual_full_elapsed, 3)},
            "three_route_offline": {**routed, "elapsed_seconds": round(annual_route_elapsed, 3), "route_counts": counts, "api_fallback": "not_measured"},
        },
        "caveats": [
            "纯 API 未在这 37 条人工 Gold 上同集实测，因此不填入本次主结果。",
            "三层路由是离线规则+年报 BERT 路由，API 兜底仅统计为未测；不把本地耗时写成完整端到端耗时。",
            "BGE 证据重排权重只在程序化 v3 开发集选择，再在人工 Gold 上验证。",
        ],
    }
    (OUT / "当前模型与自动路由比较.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    rows_csv = []
    for name, item in result["models"].items():
        rows_csv.append({"strategy": name, **{key: item.get(key) for key in ("quote_top1", "growth_exact", "growth_precision", "growth_recall", "elapsed_seconds")}})
    with (OUT / "当前模型与自动路由比较.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows_csv[0])); writer.writeheader(); writer.writerows(rows_csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
