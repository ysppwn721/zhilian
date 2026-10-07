"""Run the existing local reranker evaluator on the real-value v3 benchmark.

This wrapper keeps v3 results separate from the historical template benchmark
and adds the missing growth-set F1 and latency fields.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "答辩评测"))
import evaluate_repaired_annual_benchmark as evaluator  # noqa: E402


def with_f1(metrics: dict) -> dict:
    out = json.loads(json.dumps(metrics, ensure_ascii=False))
    for task, item in out.get("by_task", {}).items():
        if task != "growth_set":
            continue
        precision = float(item.get("source_set_precision", 0.0))
        recall = float(item.get("source_set_completeness_recall", 0.0))
        item["source_set_f1"] = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return out


def order_consistency(rows: list[dict], model: str, device: str, batch_size: int, seed: int) -> dict:
    before = evaluator.score_map(rows, evaluator.score_rows(rows, model, batch_size, device))
    shuffled_rows = evaluator.shuffled_rows(rows, seed)
    after = evaluator.score_map(shuffled_rows, evaluator.score_rows(shuffled_rows, model, batch_size, device))
    changed = 0
    for claim_id, group in evaluator.group_rows(rows).items():
        first, _ = evaluator.rank(group, before)
        second, _ = evaluator.rank(evaluator.group_rows(shuffled_rows)[claim_id], after)
        changed += int({r["fact_id"] for r in first} != {r["fact_id"] for r in second})
    return {"seed": seed, "claims": len(evaluator.group_rows(rows)), "changed_claims": changed,
            "prediction_set_consistency": 1 - changed / max(1, len(evaluator.group_rows(rows)))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "答辩评测/v3_eval_20261005")
    ap.add_argument("--model", choices=tuple(evaluator.MODELS), action="append", required=True)
    ap.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--shuffle-seed", type=int, default=20261005)
    args = ap.parse_args()
    rows = evaluator.read_rows(args.dataset)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    is_human_gold = any(bool(row.get("human_reviewed")) for row in rows)
    all_results = {"dataset": str(args.dataset), "claims": len(evaluator.group_rows(rows)),
                   "rows": len(rows), "programmatic_labels": not is_human_gold,
                   "human_gold": is_human_gold, "models": {}}
    for model in args.model:
        started = time.perf_counter()
        metrics = with_f1(evaluator.evaluate(rows, evaluator.score_map(rows, evaluator.score_rows(rows, model, args.batch_size, args.device))))
        elapsed = time.perf_counter() - started
        consistency = order_consistency(rows, model, args.device, args.batch_size, args.shuffle_seed)
        result = {"model": model, "device": "CPUExecutionProvider" if model == "bge" else args.device,
                  "elapsed_seconds": round(elapsed, 3), "metrics": metrics,
                  "candidate_order_invariance": consistency}
        (args.output_dir / f"{model}_v3_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        all_results["models"][model] = result
        print(json.dumps({"model": model, "elapsed_seconds": round(elapsed, 3),
                          "metrics": metrics["by_task"], "order": consistency}, ensure_ascii=False))
    (args.output_dir / "v3_model_comparison.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {args.output_dir / 'v3_model_comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
