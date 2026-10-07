import json
from pathlib import Path

import pytest

from 答辩评测.promote_approved_pairs import main


def write_dataset(root: Path, *, ready=True, labels=True, balanced=True):
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps({"ready_for_training": ready}, ensure_ascii=False), encoding="utf-8"
    )
    for split in ("train", "dev", "test"):
        rows = [
            {
                "claim_id": f"{split}-claim",
                "claim_text": "营收为一百万元",
                "fact_text": f"metric=营业收入；period={'本期' if split != 'test' else '上期'}",
                "group_id": split,
                "label": 1 if labels else None,
                "period": "本期" if split != "test" else "上期",
            },
            {
                "claim_id": f"{split}-claim",
                "claim_text": "营收为一百万元",
                "fact_text": "metric=营业成本；period=本期",
                "group_id": split,
                "label": 0 if labels and balanced else None,
                "period": "本期",
            },
        ]
        if split == "test":
            rows.append({
                "claim_id": f"{split}-claim",
                "claim_text": "营收为一百万元",
                "fact_text": "metric=营业收入；period=本期",
                "group_id": split,
                "label": 1 if labels else None,
                "period": "本期",
            })
        (root / f"{split}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )


def run(source: Path, out: Path) -> int:
    import sys

    old = sys.argv
    try:
        sys.argv = ["promote", "--source", str(source), "--out", str(out)]
        return main()
    finally:
        sys.argv = old


def test_rejects_unapproved_without_creating_output(tmp_path):
    source = tmp_path / "source"
    write_dataset(source, ready=False)
    out = tmp_path / "out"
    assert run(source, out) == 2
    assert not out.exists()


def test_rejects_pending_labels_without_creating_output(tmp_path):
    source = tmp_path / "source"
    write_dataset(source, ready=True, labels=False)
    out = tmp_path / "out"
    assert run(source, out) == 2
    assert not out.exists()


def test_promotes_approved_balanced_data(tmp_path):
    source = tmp_path / "source"
    write_dataset(source, ready=True)
    out = tmp_path / "out"
    assert run(source, out) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ready_for_training"] is True
    assert all((out / f"{split}.jsonl").is_file() for split in ("train", "dev", "test"))
