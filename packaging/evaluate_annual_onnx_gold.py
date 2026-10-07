"""Evaluate the annual BERT ONNX exports on the reviewed v3 Gold subset."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "答辩评测"))
import evaluate_repaired_annual_benchmark as evaluator


def score(rows, model_dir: Path, model_file: str) -> list[float]:
    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=128)
    encoded = tokenizer.encode_batch([(r["claim_text"], r["fact_text"]) for r in rows])
    session = ort.InferenceSession(str(model_dir / model_file), providers=["CPUExecutionProvider"])
    names = {item.name for item in session.get_inputs()}
    output = []
    for start in range(0, len(rows), 16):
        batch = encoded[start:start + 16]
        length = max(len(item.ids) for item in batch)
        feed = {
            "input_ids": np.asarray([item.ids + [0] * (length - len(item.ids)) for item in batch], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask + [0] * (length - len(item.ids)) for item in batch], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids + [0] * (length - len(item.ids)) for item in batch], dtype=np.int64),
        }
        result = session.run(None, {key: value for key, value in feed.items() if key in names})[0]
        output.extend((result[:, 1] - result[:, 0]).tolist())
    return [float(value) for value in output]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=ROOT / "答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl")
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--model-file", required=True)
    args = ap.parse_args()
    rows = evaluator.read_rows(args.dataset)
    started = time.perf_counter()
    scores = score(rows, args.model_dir, args.model_file)
    metrics = evaluator.evaluate(rows, evaluator.score_map(rows, scores))
    result = {"dataset": str(args.dataset), "model": str(args.model_dir / args.model_file),
              "human_gold": True, "claims": len(evaluator.group_rows(rows)),
              "elapsed_seconds": round(time.perf_counter() - started, 3), "metrics": metrics}
    out = args.model_dir / (Path(args.model_file).stem + "_human_gold_metrics.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
