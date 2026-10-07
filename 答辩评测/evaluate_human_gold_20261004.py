"""Evaluate local rerankers on the blind human-gold review batch.

The review batch is kept separate from all training data.  This script only
creates derived evaluation artifacts under ``human_gold_eval_20261004``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "答辩评测" / "semantic_review_batch_20261004"
OUT_DIR = ROOT / "答辩评测" / "human_gold_eval_20261004"
CLAIMS = REVIEW_DIR / "review_claims_completed_20261004.csv"
CANDIDATES = REVIEW_DIR / "review_candidates_completed_20261004.csv"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_repaired_annual_benchmark as evaluator
import evaluate_bge_bert_ensemble as ensemble


MODELS = ("bge", "bert_v2", "annual_repaired_v1")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def task_type(raw: str) -> str:
    if raw == "growth_set":
        return raw
    if "本期" in raw:
        return "quote_current"
    if "上期" in raw:
        return "quote_prior"
    raise ValueError(f"unrecognised review task type: {raw!r}")


def make_fact_text(row: dict[str, str]) -> str:
    if row.get("fact_text"):
        return row["fact_text"]
    return "；".join(
        f"{key}={row.get(key, '')}"
        for key in ("subject", "metric", "period", "unit", "scope")
    )


def build_dataset() -> tuple[list[dict], dict]:
    claims = read_csv(CLAIMS)
    candidates = read_csv(CANDIDATES)
    if len(claims) != 150:
        raise ValueError(f"expected 150 completed claims, got {len(claims)}")
    if len({row["review_id"] for row in claims}) != len(claims):
        raise ValueError("duplicate review_id in claims")
    by_review: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        by_review[row["review_id"]].append(row)
    rows: list[dict] = []
    claim_counts = Counter()
    source_links = 0
    for claim in claims:
        review_id = claim["review_id"]
        group = by_review.get(review_id, [])
        if not group:
            raise ValueError(f"no candidates for {review_id}")
        gold = [item for item in claim["manual_source_ids"].split(";") if item]
        expected = int(claim["expected_source_count"])
        if claim.get("manual_abstain") == "1":
            raise ValueError(f"gold batch contains an abstain label: {review_id}")
        if len(gold) != expected or len(set(gold)) != len(gold):
            raise ValueError(f"invalid gold source set for {review_id}: {gold}")
        available = {item["fact_id"] for item in group}
        if not set(gold) <= available:
            raise ValueError(f"gold fact missing from candidates: {review_id}")
        kind = task_type(claim["task_type"])
        claim_counts[kind] += 1
        source_links += len(gold)
        for candidate in sorted(group, key=lambda item: int(item["candidate_position"])):
            fact_id = candidate["fact_id"]
            rows.append({
                "claim_id": review_id,
                "company": claim["company"],
                "source_file": candidate.get("source_file_hint", ""),
                "task_type": kind,
                "claim_variant": kind,
                "claim_text": claim["claim_text"],
                "fact_id": fact_id,
                "fact_text": make_fact_text(candidate),
                "metric": candidate.get("metric", ""),
                "period": candidate.get("period", ""),
                "unit": candidate.get("unit", ""),
                "scope": candidate.get("scope", ""),
                "candidate_position": int(candidate["candidate_position"]),
                "expected_k": expected,
                "gold_fact_ids": gold,
                "label": int(candidate.get("manual_is_source") == "1"),
            })
    if sum(claim_counts.values()) != 150 or source_links != 200:
        raise ValueError(f"unexpected gold totals: {claim_counts}, links={source_links}")
    meta = {
        "claims": len(claims),
        "candidate_rows": len(rows),
        "gold_source_links": source_links,
        "task_counts": dict(claim_counts),
        "companies": len({row["company"] for row in rows}),
        "source_claims": str(CLAIMS),
        "source_candidates": str(CANDIDATES),
        "source_sha256": {str(path.name): sha256(path) for path in (CLAIMS, CANDIDATES)},
    }
    return rows, meta


def rules_score(rows: list[dict]) -> list[float]:
    """A transparent metric/period lexical baseline, with abstention on ties."""
    scores: list[float] = []
    for row in rows:
        claim = row["claim_text"]
        metric = row.get("metric", "")
        period = row.get("period", "")
        metric_hit = bool(metric and metric in claim)
        period_hit = bool(period and period in claim)
        scores.append(float(metric_hit) + 0.5 * float(period_hit))
    return scores


def evaluate_model(rows: list[dict], model: str) -> tuple[dict, float, list[float]]:
    started = time.perf_counter()
    if model == "rules":
        values = rules_score(rows)
    else:
        values = evaluator.score_rows(rows, model, batch_size=32, device="cpu")
    scores = evaluator.score_map(rows, values)
    metrics = evaluator.evaluate(rows, scores)
    return metrics, time.perf_counter() - started, values


def order_invariance(rows: list[dict], model: str, values: list[float]) -> dict:
    scores = evaluator.score_map(rows, values)
    before = {}
    for claim_id, group in evaluator.group_rows(rows).items():
        before[claim_id] = {x["fact_id"] for x in evaluator.rank(group, scores)[0]}
    shuffled = evaluator.shuffled_rows(rows, seed=20261004)
    if model == "rules":
        shuffled_values = rules_score(shuffled)
    else:
        shuffled_values = evaluator.score_rows(shuffled, model, batch_size=32, device="cpu")
    shuffled_scores = evaluator.score_map(shuffled, shuffled_values)
    changed = 0
    for claim_id, group in evaluator.group_rows(shuffled).items():
        after = {x["fact_id"] for x in evaluator.rank(group, shuffled_scores)[0]}
        changed += int(after != before[claim_id])
    return {
        "shuffle_seed": 20261004,
        "changed_claims": changed,
        "claims": len(before),
        "prediction_set_consistency": 1 - changed / max(1, len(before)),
    }


def write_chart(summary: dict) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        from PIL import Image, ImageDraw, ImageFont
        names = list(summary["models"])
        labels = ["本期 Top-1", "上期 Top-1", "增长双来源精确匹配"]
        keys = [("quote_current", "top1_accuracy"), ("quote_prior", "top1_accuracy"), ("growth_set", "source_set_exact_match")]
        colors = ["#2878B5", "#E17C05", "#2A9D8F", "#8E6BBE", "#C54B4B", "#5C677D"]
        width, height = 1800, 1050
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        def font(size: int, bold: bool = False):
            candidates = [Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf")]
            for path in candidates:
                if path.is_file():
                    return ImageFont.truetype(str(path), size=size)
            return ImageFont.load_default()
        title, label, small = font(42, True), font(24, True), font(20)
        draw.text((90, 40), "150 条人工语义 Gold：模型对比", font=title, fill="#18212B")
        draw.text((94, 100), "独立人工复核集；增长率任务要求同时选中本期与上期来源", font=small, fill="#53616F")
        left, top, chart_w, chart_h = 190, 220, 1450, 600
        bottom = top + chart_h
        for pct in range(0, 101, 20):
            y = bottom - chart_h * pct / 100
            draw.line((left, y, left + chart_w, y), fill="#DCE2E8", width=2)
            draw.text((left - 65, y - 12), f"{pct}%", font=small, fill="#65717C")
        group_w = chart_w / max(1, len(labels))
        bar_w = min(70, int(group_w / max(2, len(names) + 1)))
        gap = 8
        for i, name in enumerate(names):
            for j, (task, metric) in enumerate(keys):
                value = float(summary["models"][name][task].get(metric, 0))
                x = left + j * group_w + (group_w - (len(names) * (bar_w + gap) - gap)) / 2 + i * (bar_w + gap)
                y = bottom - chart_h * value
                draw.rectangle((x, y, x + bar_w, bottom), fill=colors[i % len(colors)])
                draw.text((x, y - 26), f"{value:.0%}", font=small, fill="#26323C")
        for j, label_text in enumerate(labels):
            draw.text((left + j * group_w + group_w / 2 - 60, bottom + 22), label_text, font=label, fill="#26323C")
        draw.line((left, bottom, left + chart_w, bottom), fill="#8D99A4", width=2)
        lx = left
        for i, name in enumerate(names):
            draw.rectangle((lx, 900, lx + 22, 922), fill=colors[i % len(colors)])
            draw.text((lx + 30, 895), name, font=small, fill="#34404B")
            lx += 250
        image.save(OUT_DIR / "human_gold_model_comparison.png", optimize=True)
        return
    names = list(summary["models"])
    labels = ["本期 Top-1", "上期 Top-1", "增长双来源精确匹配"]
    keys = [("quote_current", "top1_accuracy"), ("quote_prior", "top1_accuracy"), ("growth_set", "source_set_exact_match")]
    fig, ax = plt.subplots(figsize=(10, 5.8), dpi=160)
    width = 0.8 / max(1, len(names))
    positions = list(range(len(labels)))
    for index, name in enumerate(names):
        values = [summary["models"][name][task].get(metric, 0) for task, metric in keys]
        ax.bar([x + (index - (len(names)-1)/2) * width for x in positions], values, width=width, label=name)
    ax.set_xticks(positions, labels)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("准确率 / 精确匹配率")
    ax.set_title("150 条人工语义 Gold：本地模型对比")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=2)
    fig.text(0.01, 0.01, "独立人工复核集；增长率任务要求同时选中本期与上期来源。", fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT_DIR / "human_gold_model_comparison.png", bbox_inches="tight")
    fig.savefig(OUT_DIR / "human_gold_model_comparison.svg", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, meta = build_dataset()
    dataset_path = OUT_DIR / "human_gold_20261004.jsonl"
    dataset_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    meta["derived_dataset_sha256"] = sha256(dataset_path)
    (OUT_DIR / "manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    model_summary: dict[str, dict] = {}
    timing: dict[str, float] = {}
    invariance: dict[str, dict] = {}
    model_values: dict[str, list[float]] = {}
    for model in ("rules",) + MODELS:
        metrics, elapsed, values = evaluate_model(rows, model)
        model_summary[model] = metrics["by_task"]
        timing[model] = round(elapsed, 3)
        model_values[model] = values
        invariance[model] = order_invariance(rows, model, values)
    bge_map = evaluator.score_map(rows, model_values["bge"])
    annual_map = evaluator.score_map(rows, model_values["annual_repaired_v1"])
    fused_map, _ = ensemble.rrf_scores(rows, model_values["bge"], model_values["annual_repaired_v1"])
    model_summary["rrf_bge_annual"] = evaluator.evaluate(rows, fused_map)["by_task"]
    model_summary["task_routed"] = {
        "quote_current": model_summary["bge"]["quote_current"],
        "quote_prior": model_summary["bge"]["quote_prior"],
        "growth_set": model_summary["annual_repaired_v1"]["growth_set"],
    }
    timing["rrf_bge_annual"] = round(timing["bge"] + timing["annual_repaired_v1"], 3)
    timing["task_routed"] = timing["rrf_bge_annual"]
    invariance["rrf_bge_annual"] = {"derived_from": ["bge", "annual_repaired_v1"]}
    invariance["task_routed"] = {"derived_from": ["bge", "annual_repaired_v1"]}
    result = {
        "experiment": "human_gold_semantic_evaluation_20261004",
        "dataset": str(dataset_path),
        "human_gold": meta,
        "models": model_summary,
        "elapsed_seconds_cpu": timing,
        "candidate_order_invariance": invariance,
        "training_data_used": False,
    }
    (OUT_DIR / "human_gold_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT_DIR / "human_gold_metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["model", "quote_current_top1", "quote_prior_top1", "growth_exact", "growth_recall", "growth_precision", "elapsed_seconds", "order_consistency"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for model, tasks in model_summary.items():
            growth = tasks["growth_set"]
            writer.writerow({
                "model": model,
                "quote_current_top1": tasks["quote_current"]["top1_accuracy"],
                "quote_prior_top1": tasks["quote_prior"]["top1_accuracy"],
                "growth_exact": growth["source_set_exact_match"],
                "growth_recall": growth["source_set_completeness_recall"],
                "growth_precision": growth["source_set_precision"],
                "elapsed_seconds": timing[model],
                "order_consistency": invariance[model].get("prediction_set_consistency", "derived"),
            })
    write_chart(result)
    print(json.dumps({"output": str(OUT_DIR), "claims": meta["claims"], "candidate_rows": meta["candidate_rows"], "models": list(model_summary)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
