"""Train and evaluate a repaired weak-label reranker candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import time
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "annual_reports_weak_training_repaired_20261004"
DEFAULT_MODEL = ROOT.parent / "models" / "bert-base-chinese"
DEFAULT_OUT = ROOT.parent / "models" / "zh_reranker_bert_annual_repaired_v1"


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def audit_splits(train: list[dict], dev: list[dict], test: list[dict]) -> None:
    group_splits: dict[str, set[str]] = defaultdict(set)
    for name, rows in (("train", train), ("dev", dev), ("test", test)):
        for row in rows:
            group_splits[row["group_id"]].add(name)
    leakage = {group: sorted(names) for group, names in group_splits.items() if len(names) > 1}
    if leakage:
        raise ValueError(f"group leakage: {list(leakage.items())[:5]}")
    if not train or not dev or not test:
        raise ValueError("train/dev/test must all be non-empty")


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["claim_id"]].append(row)
    return grouped


def metrics(rows: list[dict], scores: list[float]) -> dict:
    grouped: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for row, score in zip(rows, scores):
        grouped[row["claim_id"]].append((row, score))
    by_task: dict[str, list[dict]] = defaultdict(list)
    for group in grouped.values():
        by_task[group[0][0]["task_type"]].append(group)

    output = {}
    task_scores = []
    for task_type, groups in sorted(by_task.items()):
        if task_type == "growth_set":
            exact = recall_sum = precision_sum = 0.0
            for group in groups:
                ordered = sorted(group, key=lambda pair: (-pair[1], pair[0]["fact_id"]))
                chosen = {row["fact_id"] for row, _ in ordered[:2]}
                gold = set(group[0][0]["gold_fact_ids"])
                hits = len(chosen & gold)
                exact += int(chosen == gold)
                recall_sum += hits / max(1, len(gold))
                precision_sum += hits / max(1, len(chosen))
            score = exact / max(1, len(groups))
            output[task_type] = {
                "claims": len(groups),
                "source_set_exact_match": score,
                "source_set_completeness_recall": recall_sum / max(1, len(groups)),
                "source_set_precision": precision_sum / max(1, len(groups)),
            }
        else:
            correct = 0
            for group in groups:
                chosen = max(group, key=lambda pair: (pair[1], pair[0]["fact_id"]))[0]
                correct += int(chosen["label"] == 1)
            score = correct / max(1, len(groups))
            output[task_type] = {
                "claims": len(groups),
                "top1_accuracy": score,
                "error_association_rate": 1 - score,
            }
        task_scores.append(score)
    return {
        "by_task": output,
        "macro_task_score": sum(task_scores) / max(1, len(task_scores)),
        "claims": len(grouped),
    }


def score_rows(rows: list[dict], model, tokenizer, device, batch_size: int, max_length: int) -> list[float]:
    import torch

    scores: list[float] = []
    model.eval()
    with torch.inference_mode():
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            encoded = tokenizer(
                [row["claim_text"] for row in batch],
                [row["fact_text"] for row in batch],
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(device) for key, value in encoded.items()}
            logits = model(**encoded).logits
            if logits.shape[-1] == 1:
                values = logits[:, 0]
            else:
                values = logits[:, 1] - logits[:, 0]
            scores.extend(float(value) for value in values.detach().cpu())
    return scores


def collate(items, tokenizer, device, max_length: int):
    import torch

    claims, facts, labels = zip(*items)
    encoded = tokenizer(
        list(claims), list(facts), padding=True, truncation=True,
        max_length=max_length, return_tensors="pt",
    )
    encoded["labels"] = torch.tensor(labels, dtype=torch.long)
    return {key: value.to(device) for key, value in encoded.items()}


def order_stability(rows: list[dict], model, tokenizer, device, batch_size: int, max_length: int, seed: int) -> dict:
    import torch

    grouped = group_rows(rows)
    by_task: dict[str, list[str]] = defaultdict(list)
    for claim_id, group in grouped.items():
        by_task[group[0]["task_type"]].append(claim_id)
    rng = random.Random(seed)
    selected = []
    for task_type in sorted(by_task):
        ids = sorted(by_task[task_type])
        rng.shuffle(ids)
        selected.extend(ids[:16])
    selected_set = set(selected)
    sample = [row for row in rows if row["claim_id"] in selected_set]
    shuffled = list(sample)
    rng.shuffle(shuffled)
    before = score_rows(sample, model, tokenizer, device, batch_size, max_length)
    after = score_rows(shuffled, model, tokenizer, device, batch_size, max_length)
    before_scores = {(row["claim_id"], row["fact_id"]): score for row, score in zip(sample, before)}
    after_scores = {(row["claim_id"], row["fact_id"]): score for row, score in zip(shuffled, after)}
    max_delta = max((abs(before_scores[key] - after_scores[key]) for key in before_scores), default=0.0)
    sample_groups = group_rows(sample)
    shuffled_groups = group_rows(shuffled)
    changed = 0
    for claim_id, group in sample_groups.items():
        k = int(group[0]["expected_k"])
        first = sorted(group, key=lambda row: (-before_scores[(claim_id, row["fact_id"])], row["fact_id"]))[:k]
        second = sorted(
            shuffled_groups[claim_id],
            key=lambda row: (-after_scores[(claim_id, row["fact_id"])], row["fact_id"]),
        )[:k]
        changed += int({row["fact_id"] for row in first} != {row["fact_id"] for row in second})
    return {
        "claims": len(sample_groups),
        "rows": len(sample),
        "max_absolute_score_delta": max_delta,
        "prediction_set_changed_claims": changed,
        "prediction_set_consistency": 1 - changed / max(1, len(sample_groups)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20261004)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite existing model output: {args.output}")
    dataset_manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
    if dataset_manifest.get("ready_for_training") is not True:
        raise SystemExit("Dataset manifest does not allow training")

    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cuda.matmul.allow_tf32 = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    train = read_rows(args.data / "train.jsonl")
    dev = read_rows(args.data / "dev.jsonl")
    test = read_rows(args.data / "test.jsonl")
    audit_splits(train, dev, test)

    class PairDataset(Dataset):
        def __init__(self, rows):
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, index):
            row = self.rows[index]
            return row["claim_text"], row["fact_text"], int(row["label"])

    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model, num_labels=2, local_files_only=True
    )
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
    train_loader = DataLoader(
        PairDataset(train), batch_size=args.batch_size, shuffle=True,
        collate_fn=lambda items: collate(items, tokenizer, device, args.max_length),
    )

    history = []
    best_score = float("-inf")
    best_state = None
    start_time = time.perf_counter()
    for epoch in range(args.epochs):
        model.train()
        total_loss = 0.0
        for batch in train_loader:
            optimizer.zero_grad(set_to_none=True)
            loss = model(**batch).loss
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach().cpu())
        dev_scores = score_rows(dev, model, tokenizer, device, args.batch_size, args.max_length)
        dev_metrics = metrics(dev, dev_scores)
        epoch_row = {
            "epoch": epoch + 1,
            "train_loss": total_loss / max(1, len(train_loader)),
            "dev": dev_metrics,
        }
        history.append(epoch_row)
        if dev_metrics["macro_task_score"] > best_score:
            best_score = dev_metrics["macro_task_score"]
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
        print(json.dumps(epoch_row, ensure_ascii=False), flush=True)

    if best_state is None:
        raise RuntimeError("No model checkpoint was selected")
    model.load_state_dict(best_state)
    model.to(device)
    test_scores = score_rows(test, model, tokenizer, device, args.batch_size, args.max_length)
    test_metrics = metrics(test, test_scores)
    stability = order_stability(test, model, tokenizer, device, args.batch_size, args.max_length, args.seed)

    args.output.mkdir(parents=True)
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    training_manifest = {
        "base_model": str(args.model),
        "output_model": str(args.output),
        "dataset": str(args.data),
        "label_quality": dataset_manifest["label_quality"],
        "device": str(device),
        "device_name": torch.cuda.get_device_name(0) if device.type == "cuda" else "CPU",
        "epochs_requested": args.epochs,
        "selected_epoch": max(
            (row for row in history), key=lambda row: row["dev"]["macro_task_score"]
        )["epoch"],
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "train_rows": len(train),
        "dev_rows": len(dev),
        "test_rows": len(test),
        "dev_best_macro_task_score": best_score,
        "test_metrics": test_metrics,
        "candidate_order_stability_test": stability,
        "history": history,
        "elapsed_seconds_including_eval_and_save": round(time.perf_counter() - start_time, 2),
        "warning": "Weak-label research candidate only; not human-gold performance and not production-approved.",
    }
    (args.output / "training_manifest.json").write_text(
        json.dumps(training_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"selected_epoch": training_manifest["selected_epoch"], "test": test_metrics,
                      "order_stability": stability, "output": str(args.output)},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
