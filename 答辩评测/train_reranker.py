"""Train a cross-encoder reranker on auditable claim/fact pairs.

The script deliberately requires separate train and evaluation files. It will
fail when a group_id appears in both train and dev/test, so the frozen real
evaluation set cannot be accidentally used as training data.

Expected JSONL fields: claim_text, fact_text, label, group_id.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    required = {"claim_text", "fact_text", "label", "group_id"}
    missing = required - set(rows[0]) if rows else required
    if missing:
        raise ValueError(f"{path} 缺少字段: {sorted(missing)}")
    return rows


def audit_splits(train: list[dict], dev: list[dict], test: list[dict]) -> None:
    groups = {}
    for name, rows in (("train", train), ("dev", dev), ("test", test)):
        for row in rows:
            group = row["group_id"]
            previous = groups.setdefault(group, set())
            previous.add(name)
    leakage = {group: sorted(names) for group, names in groups.items() if len(names) > 1}
    if leakage:
        sample = list(leakage.items())[:5]
        raise ValueError(f"发现文档组泄漏 {len(leakage)} 组，示例: {sample}")
    if not train or not dev or not test:
        raise ValueError("train/dev/test 均必须有数据；不能用冻结评测集替代训练集")


def audit_manifests(paths: list[Path]) -> None:
    for directory in {path.parent for path in paths}:
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("ready_for_training") is False:
            raise ValueError(f"Dataset is not approved for training: {directory}")


def report(name: str, rows: list[dict]) -> None:
    positives = sum(int(row["label"]) == 1 for row in rows)
    groups = len({row["group_id"] for row in rows})
    print(f"{name}: rows={len(rows)}, positives={positives}, groups={groups}")


def ranking_metrics(rows: list[dict], scores: list[float]) -> dict:
    grouped = defaultdict(list)
    for row, score in zip(rows, scores):
        grouped[row["claim_id"]].append((score, int(row["label"])))
    top1 = sum(max(items)[1] == 1 for items in grouped.values()) / max(1, len(grouped))
    return {
        "claims": len(grouped),
        "top1_accuracy": round(top1, 6),
        "error_association_rate": round(1.0 - top1, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Transformers 基础模型目录或 Hugging Face ID")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    args = parser.parse_args()

    audit_manifests([args.train, args.dev, args.test])
    train = read_rows(args.train)
    dev = read_rows(args.dev)
    test = read_rows(args.test)
    audit_splits(train, dev, test)
    report("train", train)
    report("dev", dev)
    report("test", test)

    # Imports stay here so the data-audit behavior remains usable before the
    # optional PyTorch/Transformers environment is installed.
    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    class PairDataset(Dataset):
        def __init__(self, rows):
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, index):
            row = self.rows[index]
            return row["claim_text"], row["fact_text"], int(row["label"])

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForSequenceClassification.from_pretrained(args.model, num_labels=2)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)

    def batchify(items):
        claims, facts, labels = zip(*items)
        encoded = tokenizer(list(claims), list(facts), padding=True,
                            truncation=True, max_length=args.max_length,
                            return_tensors="pt")
        encoded["labels"] = torch.tensor(labels, dtype=torch.long)
        return {key: value.to(device) for key, value in encoded.items()}

    train_loader = DataLoader(PairDataset(train), batch_size=args.batch_size,
                              shuffle=True, collate_fn=batchify)
    model.train()
    history = []
    for epoch in range(args.epochs):
        total = 0.0
        for batch in train_loader:
            optimizer.zero_grad()
            loss = model(**batch).loss
            loss.backward()
            optimizer.step()
            total += float(loss.detach().cpu())
        epoch_loss = total / max(1, len(train_loader))
        history.append({"epoch": epoch + 1, "train_loss": epoch_loss})
        print(f"epoch={epoch + 1} loss={epoch_loss:.4f}")

    def score_rows(rows):
        model.eval()
        scores = []
        with torch.no_grad():
            for start in range(0, len(rows), args.batch_size):
                batch_rows = rows[start:start + args.batch_size]
                encoded = tokenizer([r["claim_text"] for r in batch_rows],
                                    [r["fact_text"] for r in batch_rows],
                                    padding=True, truncation=True,
                                    max_length=args.max_length, return_tensors="pt")
                encoded = {key: value.to(device) for key, value in encoded.items()}
                logits = model(**encoded).logits
                scores.extend(logits[:, 1].detach().cpu().tolist())
        return scores

    metrics = {}
    for name, rows in (("dev", dev), ("test", test)):
        metrics[name] = ranking_metrics(rows, score_rows(rows))
        print(f"{name}_metrics={json.dumps(metrics[name], ensure_ascii=False)}")
    history[-1]["dev"] = metrics["dev"]
    history[-1]["test"] = metrics["test"]

    args.output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    (args.output / "training_manifest.json").write_text(
        json.dumps({"train": str(args.train), "dev": str(args.dev),
                    "test": str(args.test), "epochs": args.epochs,
                    "batch_size": args.batch_size, "metrics": metrics,
                    "history": history},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
