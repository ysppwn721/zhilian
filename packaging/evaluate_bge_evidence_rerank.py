"""Evaluate a conservative BGE + deterministic evidence re-ranker.

BGE supplies semantic scores. The added features are evidence checks already
available to the deterministic engine: metric/period/unit text and a
displayed numeric value matching the candidate fact after unit conversion.
This is an offline experiment; it does not change production routing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "答辩评测"))
import evaluate_repaired_annual_benchmark as evaluator  # noqa: E402

NUM_RE = re.compile(r"(?<![\d.])-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")
UNIT_RE = re.compile(r"亿元|万元|元/股|元|%|件|人")
UNITS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000"), "%": Decimal("1"), "元/股": Decimal("1")}
PERIODS = {
    "本期": ("本期", "本报告期", "报告期内", "报告期", "本年度", "本年", "当期", "今年"),
    "上期": ("上期", "上年度同期", "上年同期", "去年同期", "上年度", "上年", "去年"),
}


def _number(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def claim_numbers(text: str) -> list[tuple[Decimal, str | None]]:
    result = []
    for match in NUM_RE.finditer(text or ""):
        value = _number(match.group())
        if value is None:
            continue
        tail = text[match.end():match.end() + 5]
        unit_match = UNIT_RE.match(tail)
        result.append((value, unit_match.group() if unit_match else None))
    return result


def metric_hit(row: dict, text: str) -> float:
    metric = str(row.get("metric") or "")
    alias = evaluator.CONTROLLED_ALIASES.get(metric, metric)
    options = {item for item in (metric, alias) if item}
    options |= {re.sub(r"[（(].*?[）)]", "", item) for item in options}
    return 1.0 if any(item in text for item in options if item) else 0.0


def period_hit(row: dict, text: str) -> float:
    period = str(row.get("period") or "")
    return 1.0 if any(token in text for token in PERIODS.get(period, (period,))) else 0.0


def unit_hit(row: dict, text: str) -> float:
    unit = str(row.get("unit") or "")
    return 1.0 if unit and unit in text else 0.0


def value_hit(row: dict, text: str) -> float:
    fact_value = row.get("fact_value")
    fact_unit = str(row.get("unit") or "")
    if fact_value is None or fact_unit not in UNITS:
        return 0.0
    try:
        target = Decimal(str(fact_value)) * UNITS[fact_unit]
    except (InvalidOperation, ValueError):
        return 0.0
    for value, display_unit in claim_numbers(text):
        if display_unit not in UNITS:
            continue
        observed = value * UNITS[display_unit]
        tolerance = max(Decimal("0.02"), abs(target) * Decimal("0.000002"))
        if abs(observed - target) <= tolerance:
            return 1.0
    return 0.0


def adjust(row: dict, base: float, weights: tuple[float, float, float, float]) -> float:
    wm, wp, wu, wv = weights
    text = str(row.get("claim_text") or "")
    return base + wm * metric_hit(row, text) + wp * period_hit(row, text) + wu * unit_hit(row, text) + wv * value_hit(row, text)


def rerank(rows: list[dict], scores: list[float], weights: tuple[float, float, float, float]) -> dict[tuple[str, str], float]:
    return {(row["claim_id"], row["fact_id"]): adjust(row, score, weights)
            for row, score in zip(rows, scores)}


def evaluate_weights(rows: list[dict], scores: list[float], weights: tuple[float, float, float, float]) -> dict:
    metrics = evaluator.evaluate(rows, rerank(rows, scores, weights))
    quote_metrics = metrics["by_task"].get("quote_current", {})
    growth_metrics = metrics["by_task"].get("growth_set", {})
    precision = growth_metrics.get("source_set_precision", 0.0)
    recall = growth_metrics.get("source_set_completeness_recall", 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"weights": list(weights),
            "quote_top1": quote_metrics.get("top1_accuracy", 0.0),
            "growth_exact": growth_metrics.get("source_set_exact_match", 0.0),
            "growth_f1": f1,
            "metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=Path, default=ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl")
    ap.add_argument("--gold", type=Path, default=ROOT / "答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl")
    ap.add_argument("--output", type=Path, default=ROOT / "答辩评测/v3_eval_20261005/v3_human_gold_40/bge_evidence_rerank_experiment.json")
    args = ap.parse_args()
    dev = evaluator.read_rows(args.dev)
    gold = evaluator.read_rows(args.gold)
    print("scoring development set with BGE ...")
    # Keep the same batch size as the existing v3 BGE report. The ONNX export
    # is sensitive to dynamic padding length, so changing it changes scores.
    dev_scores = evaluator.score_rows(dev, "bge", 16, "cpu")
    print("scoring human Gold with BGE ...")
    gold_scores = evaluator.score_rows(gold, "bge", 16, "cpu")
    candidates = []
    for wm in (0.0, 0.5, 1.0, 1.5, 2.0):
        for wp in (0.0, 0.25, 0.5, 0.75, 1.0):
            for wu in (0.0, 0.25, 0.5, 0.75, 1.0):
                for wv in (0.0, 0.75, 1.5, 2.25, 3.0):
                    candidates.append(evaluate_weights(dev, dev_scores, (wm, wp, wu, wv)))
    best = max(candidates, key=lambda item: (item["growth_exact"], item["quote_top1"], item["growth_f1"]))
    heldout = evaluate_weights(gold, gold_scores, tuple(best["weights"]))
    base_dev = evaluate_weights(dev, dev_scores, (0.0, 0.0, 0.0, 0.0))
    base_gold = evaluate_weights(gold, gold_scores, (0.0, 0.0, 0.0, 0.0))
    output = {
        "method": "BGE raw score + metric/period/unit/value evidence",
        "development_dataset": str(args.dev),
        "human_gold_dataset": str(args.gold),
        "weight_selection": "fixed grid selected on programmatic v3 only",
        "base_bge_development": base_dev,
        "best_development": best,
        "base_bge_human_gold": base_gold,
        "heldout_human_gold": heldout,
        "caveats": [
            "开发集标签是程序化弱标签；只用于选择固定权重，不作为人工准确率。",
            "人工 Gold 仅 37 条可评分题，3 条人工拒答；结果用于方向判断，不代表全行业泛化。",
            "这是离线排序实验，尚未接入生产 BGE 路由。",
        ],
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"weights": best["weights"],
                      "dev": {k: best[k] for k in ("quote_top1", "growth_exact", "growth_f1")},
                      "gold_base": {k: base_gold[k] for k in ("quote_top1", "growth_exact", "growth_f1")},
                      "gold_heldout": {k: heldout[k] for k in ("quote_top1", "growth_exact", "growth_f1")},
                      "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
