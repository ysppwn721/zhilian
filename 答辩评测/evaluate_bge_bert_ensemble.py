"""Evaluate equal-weight BGE + annual-repaired-BERT reciprocal-rank fusion."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "答辩评测"
DATA_DIR = EVAL_DIR / "annual_benchmark_repaired_20261004_v2"
OUT_JSON = DATA_DIR / "metrics_bge_bert_rrf.json"
OUT_CSV = DATA_DIR / "bge_bert_rrf_comparison.csv"
OUT_PAIRS = DATA_DIR / "pair_scores_bge_bert_rrf.csv"
OUT_REPORT = DATA_DIR / "双模型融合评测.md"
OUT_CHART = DATA_DIR / "bge_bert_rrf_comparison.png"
RRF_K = 60

sys.path.insert(0, str(EVAL_DIR))
import evaluate_repaired_annual_benchmark as evaluator


def average_ranks(group: list[dict], score_by_fact: dict[str, float]) -> dict[str, float]:
    ordered = sorted(group, key=lambda row: (-score_by_fact[row["fact_id"]], row["fact_id"]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        score = score_by_fact[ordered[index]["fact_id"]]
        while end < len(ordered) and math.isclose(
            score, score_by_fact[ordered[end]["fact_id"]], rel_tol=1e-6, abs_tol=1e-7
        ):
            end += 1
        average_rank = ((index + 1) + end) / 2
        for row in ordered[index:end]:
            ranks[row["fact_id"]] = average_rank
        index = end
    return ranks


def rrf_scores(
    rows: list[dict], bge_scores: list[float], bert_scores: list[float]
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], tuple[float, float, float, float, float]]]:
    grouped = evaluator.group_rows(rows)
    bge_map = evaluator.score_map(rows, bge_scores)
    bert_map = evaluator.score_map(rows, bert_scores)
    fused: dict[tuple[str, str], float] = {}
    details: dict[tuple[str, str], tuple[float, float, float, float, float]] = {}
    for group in grouped.values():
        claim_id = group[0]["claim_id"]
        bge_rank = average_ranks(
            group, {row["fact_id"]: bge_map[(claim_id, row["fact_id"])] for row in group}
        )
        bert_rank = average_ranks(
            group, {row["fact_id"]: bert_map[(claim_id, row["fact_id"])] for row in group}
        )
        for row in group:
            key = (claim_id, row["fact_id"])
            score = 1 / (RRF_K + bge_rank[row["fact_id"]]) + 1 / (RRF_K + bert_rank[row["fact_id"]])
            fused[key] = score
            details[key] = (bge_map[key], bge_rank[row["fact_id"]], bert_map[key], bert_rank[row["fact_id"]], score)
    return fused, details


def prediction_sets(rows: list[dict], scores: dict[tuple[str, str], float]) -> dict[str, set[str]]:
    predictions = {}
    for claim_id, group in evaluator.group_rows(rows).items():
        selected, _ = evaluator.rank(group, scores)
        predictions[claim_id] = {row["fact_id"] for row in selected}
    return predictions


def claim_correctness(rows: list[dict], scores: dict[tuple[str, str], float]) -> dict[str, bool]:
    correctness = {}
    for claim_id, group in evaluator.group_rows(rows).items():
        selected, tied = evaluator.rank(group, scores)
        if group[0]["task_type"] == "growth_set":
            correctness[claim_id] = (
                {row["fact_id"] for row in selected} == set(group[0]["gold_fact_ids"]) and not tied
            )
        else:
            correctness[claim_id] = len(selected) == 1 and selected[0]["label"] == 1 and not tied
    return correctness


def comparison_by_task(
    rows: list[dict], fused: dict[tuple[str, str], float], baseline: dict[tuple[str, str], float]
) -> dict[str, dict[str, int]]:
    fused_correct = claim_correctness(rows, fused)
    baseline_correct = claim_correctness(rows, baseline)
    grouped = evaluator.group_rows(rows)
    counts: dict[str, dict[str, int]] = {}
    for claim_id, group in grouped.items():
        task = group[0]["task_type"]
        row = counts.setdefault(task, {"claims": 0, "fusion_wins": 0, "ties": 0, "fusion_losses": 0})
        row["claims"] += 1
        left, right = fused_correct[claim_id], baseline_correct[claim_id]
        row["fusion_wins"] += int(left and not right)
        row["fusion_losses"] += int(right and not left)
        row["ties"] += int(left == right)
    return counts


def write_pair_csv(rows: list[dict], details: dict[tuple[str, str], tuple[float, float, float, float, float]]) -> None:
    fields = [
        "claim_id", "task_type", "claim_text", "fact_id", "fact_text", "label", "gold_fact_ids",
        "bge_score", "bge_rank", "bert_score", "bert_rank", "rrf_score",
    ]
    with OUT_PAIRS.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            bge, bge_rank, bert, bert_rank, fused = details[(row["claim_id"], row["fact_id"])]
            writer.writerow({
                **{key: row.get(key, "") for key in fields[:7]},
                "gold_fact_ids": ";".join(row.get("gold_fact_ids", [])),
                "bge_score": f"{bge:.9g}",
                "bge_rank": f"{bge_rank:.3f}",
                "bert_score": f"{bert:.9g}",
                "bert_rank": f"{bert_rank:.3f}",
                "rrf_score": f"{fused:.12g}",
            })


def font(size: int, bold: bool = False):
    candidates = (
        [Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/msyh.ttc")]
        if bold else [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf")]
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def write_chart(results: dict) -> None:
    labels = ["BGE", "年报修正版 BERT", "BGE + BERT 等权 RRF"]
    keys = ["quote_current", "quote_prior", "growth_set"]
    metric_labels = ["本期引用 Top-1", "上期引用 Top-1", "增长来源集 Top-2 精确匹配"]
    colors = ["#16877B", "#D98236", "#5968B0"]
    width, height = 1800, 1040
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    title, sub, label, tick, small = font(46, True), font(25), font(21, True), font(19), font(18)
    draw.text((95, 48), "BGE 与年报微调 BERT：等权 RRF 融合", font=title, fill="#18212B")
    draw.text((99, 112), "同一批 1,275 道程序化弱标签诊断题；k=60，固定参数，不在本集调权", font=sub, fill="#53616F")
    lx = 110
    for color, text in zip(colors, metric_labels):
        draw.rounded_rectangle((lx, 173, lx + 24, 197), radius=4, fill=color)
        draw.text((lx + 35, 170), text, font=small, fill="#34404B")
        lx += 460
    left, top, chart_w, chart_h = 180, 250, 1450, 555
    bottom = top + chart_h
    for pct in range(0, 101, 20):
        y = bottom - chart_h * pct / 100
        draw.line((left, y, left + chart_w, y), fill="#DCE2E8", width=2)
        draw.text((left - 66, y - 12), f"{pct}%", font=tick, fill="#65717C")
    values = [
        [results["bge"]["by_task"][key].get("top1_accuracy", results["bge"]["by_task"][key].get("source_set_exact_match")) for key in keys],
        [results["bert"]["by_task"][key].get("top1_accuracy", results["bert"]["by_task"][key].get("source_set_exact_match")) for key in keys],
        [results["fusion"]["by_task"][key].get("top1_accuracy", results["fusion"]["by_task"][key].get("source_set_exact_match")) for key in keys],
    ]
    group_w = chart_w / 3
    bar_w, gap = 95, 24
    for model_index, (model_label, model_values) in enumerate(zip(labels, values)):
        block_w = bar_w * 3 + gap * 2
        start_x = left + model_index * group_w + (group_w - block_w) / 2
        for metric_index, (value, color) in enumerate(zip(model_values, colors)):
            x = start_x + metric_index * (bar_w + gap)
            y = bottom - chart_h * value
            draw.rounded_rectangle((x, y, x + bar_w, bottom), radius=7, fill=color)
            text = f"{value:.1%}"
            bbox = draw.textbbox((0, 0), text, font=label)
            draw.text((x + (bar_w - (bbox[2] - bbox[0])) / 2, y - 31), text, font=label, fill="#26323C")
        bbox = draw.textbbox((0, 0), model_label, font=label)
        draw.text((left + model_index * group_w + (group_w - (bbox[2] - bbox[0])) / 2, bottom + 21), model_label, font=label, fill="#26323C")
    draw.line((left, bottom, left + chart_w, bottom), fill="#8D99A4", width=2)
    draw.text((100, 870), "RRF：各模型在每道题内独立排序，再按 1/(60+名次) 等权累加；不直接混加原始分数。", font=small, fill="#34404B")
    draw.text((100, 904), "增长题要求 Top-2 同时选中本期与上期来源。融合仅用于候选排序，不能替代数值/口径校验。", font=small, fill="#34404B")
    draw.text((100, 938), "诊断标签由 PDF 解析与规则生成，非人工金标准；BGE 为 CPU ONNX，BERT 为 RTX 5060 CUDA。", font=small, fill="#34404B")
    image.save(OUT_CHART, optimize=True)


def export_scores(model: str, output_path: Path) -> int:
    rows = evaluator.read_rows(DATA_DIR / "benchmark.jsonl")
    device = "cpu" if model == "bge" else "cuda"
    started = time.perf_counter()
    values = evaluator.score_rows(rows, model, batch_size=64, device=device)
    payload = {
        "model": model,
        "device": "CPUExecutionProvider" if model == "bge" else "cuda",
        "dataset": str(DATA_DIR / "benchmark.jsonl"),
        "rows": len(rows),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "scores": [
            {"claim_id": row["claim_id"], "fact_id": row["fact_id"], "score": score}
            for row, score in zip(rows, values)
        ],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"model": model, "rows": len(rows), "seconds": payload["elapsed_seconds"], "output": str(output_path)}, ensure_ascii=False))
    return 0


def load_score_file(path: Path, expected_model: str, rows: list[dict]) -> tuple[list[float], float, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("model") != expected_model:
        raise ValueError(f"{path} contains {payload.get('model')}, expected {expected_model}")
    if len(payload.get("scores", [])) != len(rows):
        raise ValueError(f"{path} row count does not match benchmark")
    values = []
    for row, scored in zip(rows, payload["scores"]):
        if (row["claim_id"], row["fact_id"]) != (scored["claim_id"], scored["fact_id"]):
            raise ValueError(f"{path} candidate order/key mismatch")
        values.append(float(scored["score"]))
    return values, float(payload["elapsed_seconds"]), str(payload["device"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-model", choices=("bge", "annual_repaired_v1"))
    parser.add_argument("--score-out", type=Path)
    parser.add_argument("--bge-score-file", type=Path)
    parser.add_argument("--bert-score-file", type=Path)
    args = parser.parse_args()
    if args.score_model:
        if not args.score_out:
            parser.error("--score-model requires --score-out")
        return export_scores(args.score_model, args.score_out)
    if bool(args.bge_score_file) != bool(args.bert_score_file):
        parser.error("provide both --bge-score-file and --bert-score-file")
    rows = evaluator.read_rows(DATA_DIR / "benchmark.jsonl")
    started = time.perf_counter()
    if args.bge_score_file:
        bge_values, bge_seconds, bge_device = load_score_file(args.bge_score_file, "bge", rows)
        bert_values, bert_seconds, bert_device = load_score_file(args.bert_score_file, "annual_repaired_v1", rows)
    else:
        score_started = time.perf_counter()
        bge_values = evaluator.score_rows(rows, "bge", batch_size=64, device="cpu")
        bge_seconds = time.perf_counter() - score_started
        score_started = time.perf_counter()
        bert_values = evaluator.score_rows(rows, "annual_repaired_v1", batch_size=64, device="cuda")
        bert_seconds = time.perf_counter() - score_started
        bge_device, bert_device = "CPUExecutionProvider", "cuda"

    bge = evaluator.score_map(rows, bge_values)
    bert = evaluator.score_map(rows, bert_values)
    fused, details = rrf_scores(rows, bge_values, bert_values)
    bge_metrics = evaluator.evaluate(rows, bge)
    bert_metrics = evaluator.evaluate(rows, bert)
    fused_metrics = evaluator.evaluate(rows, fused)

    before = prediction_sets(rows, fused)
    shuffled = evaluator.shuffled_rows(rows, seed=20261004)
    after = prediction_sets(shuffled, fused)
    changed = sum(before[claim_id] != after[claim_id] for claim_id in before)

    grouped = evaluator.group_rows(rows)
    quote_agree = growth_agree = quote_n = growth_n = 0
    bge_predictions = prediction_sets(rows, bge)
    bert_predictions = prediction_sets(rows, bert)
    for claim_id, group in grouped.items():
        if group[0]["task_type"] == "growth_set":
            growth_n += 1
            growth_agree += int(bge_predictions[claim_id] == bert_predictions[claim_id])
        else:
            quote_n += 1
            quote_agree += int(bge_predictions[claim_id] == bert_predictions[claim_id])

    wins_vs_bge = comparison_by_task(rows, fused, bge)
    wins_vs_bert = comparison_by_task(rows, fused, bert)
    output = {
        "experiment": "equal_weight_reciprocal_rank_fusion",
        "models": ["bge", "annual_repaired_v1"],
        "fusion": {
            "method": "tie_aware_reciprocal_rank_fusion",
            "k": RRF_K,
            "weights": {"bge": 1.0, "annual_repaired_v1": 1.0},
            "rank_definition": "1-based average rank for score ties within rel_tol=1e-6, abs_tol=1e-7",
        },
        "dataset": str(DATA_DIR / "benchmark.jsonl"),
        "programmatic_labels": True,
        "rows": len(rows),
        "claims": len(grouped),
        "companies": 19,
        "reports": 36,
        "device": {"bge": bge_device, "annual_repaired_v1": f"{bert_device} (RTX 5060)"},
        "elapsed_seconds": {
            "bge_inference": round(bge_seconds, 2),
            "bert_inference": round(bert_seconds, 2),
            "both_models_inference_sum": round(bge_seconds + bert_seconds, 2),
            "fusion_and_exports": round(time.perf_counter() - started, 2),
            "serial_pipeline_estimate": round(bge_seconds + bert_seconds + (time.perf_counter() - started), 2),
        },
        "model_metrics": {"bge": bge_metrics, "bert": bert_metrics, "fusion": fused_metrics},
        "fusion_vs_single_model": {"bge": wins_vs_bge, "bert": wins_vs_bert},
        "model_top_prediction_agreement": {
            "single_value_claims": quote_n,
            "single_value_top1_agreement": quote_agree / max(1, quote_n),
            "growth_claims": growth_n,
            "growth_top2_set_agreement": growth_agree / max(1, growth_n),
        },
        "candidate_order_invariance": {
            "shuffle_seed": 20261004,
            "prediction_set_changed_claims": changed,
            "prediction_set_consistency": 1 - changed / max(1, len(before)),
        },
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_pair_csv(rows, details)
    write_chart({
        "bge": bge_metrics,
        "bert": bert_metrics,
        "fusion": fused_metrics,
    })

    metric_rows = []
    for model, metrics in (("BGE", bge_metrics), ("年报修正版 BERT", bert_metrics), ("BGE + BERT 等权 RRF", fused_metrics)):
        tasks = metrics["by_task"]
        metric_rows.append({
            "model": model,
            "quote_current_top1": tasks["quote_current"]["top1_accuracy"],
            "quote_prior_top1": tasks["quote_prior"]["top1_accuracy"],
            "growth_source_set_top2_exact": tasks["growth_set"]["source_set_exact_match"],
            "growth_source_set_completeness": tasks["growth_set"]["source_set_completeness_recall"],
        })
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(metric_rows[0]))
        writer.writeheader()
        writer.writerows(metric_rows)

    fusion = fused_metrics["by_task"]
    report = f'''# BGE 与年报微调 BERT 双模型融合评测

## 实验设计

- 使用同一批 {len(grouped):,} 道题、{len(rows):,} 个候选对；来源为 36 份年报、19 家公司。
- 参与模型：BGE Reranker v2-m3（CPU ONNX）与年报修正版 BERT（RTX 5060 CUDA）。
- 融合：逐题分别按模型分数排序，使用等权 RRF：`1/(60 + BGE名次) + 1/(60 + BERT名次)`。参数 `k=60` 预先固定，不根据本诊断集调权；同分使用平均名次。
- 单值题分别报告本期、上期 Top-1；增长题报告 Top-2 来源集合精确匹配与完整率。

## 结果

| 模型 | 本期引用 Top-1 | 上期引用 Top-1 | 增长来源集 Top-2 精确匹配 | 增长来源完整率 |
|---|---:|---:|---:|---:|
| BGE | {bge_metrics['by_task']['quote_current']['top1_accuracy']:.2%} | {bge_metrics['by_task']['quote_prior']['top1_accuracy']:.2%} | {bge_metrics['by_task']['growth_set']['source_set_exact_match']:.2%} | {bge_metrics['by_task']['growth_set']['source_set_completeness_recall']:.2%} |
| 年报修正版 BERT | {bert_metrics['by_task']['quote_current']['top1_accuracy']:.2%} | {bert_metrics['by_task']['quote_prior']['top1_accuracy']:.2%} | {bert_metrics['by_task']['growth_set']['source_set_exact_match']:.2%} | {bert_metrics['by_task']['growth_set']['source_set_completeness_recall']:.2%} |
| **等权 RRF 融合** | **{fusion['quote_current']['top1_accuracy']:.2%}** | **{fusion['quote_prior']['top1_accuracy']:.2%}** | **{fusion['growth_set']['source_set_exact_match']:.2%}** | **{fusion['growth_set']['source_set_completeness_recall']:.2%}** |

图表：`bge_bert_rrf_comparison.png`。可编辑汇总：`bge_bert_rrf_comparison.csv`。逐候选原始分数、模型平均名次与 RRF 得分：`pair_scores_bge_bert_rrf.csv`。

## 结果解读

- 等权融合相对 BGE：本期、上期引用分别下降 {fusion['quote_current']['top1_accuracy'] - bge_metrics['by_task']['quote_current']['top1_accuracy']:+.2%}、{fusion['quote_prior']['top1_accuracy'] - bge_metrics['by_task']['quote_prior']['top1_accuracy']:+.2%}；增长来源集精确匹配提高 {fusion['growth_set']['source_set_exact_match'] - bge_metrics['by_task']['growth_set']['source_set_exact_match']:+.2%}。
- 等权融合相对修正版 BERT：本期、上期引用分别提高 {fusion['quote_current']['top1_accuracy'] - bert_metrics['by_task']['quote_current']['top1_accuracy']:+.2%}、{fusion['quote_prior']['top1_accuracy'] - bert_metrics['by_task']['quote_prior']['top1_accuracy']:+.2%}；增长来源集精确匹配下降 {fusion['growth_set']['source_set_exact_match'] - bert_metrics['by_task']['growth_set']['source_set_exact_match']:+.2%}。
- 融合引入了本期 {fusion['quote_current']['ambiguous_top1']} 道、上期 {fusion['quote_prior']['ambiguous_top1']} 道 Top-1 并列，以及增长题 {fusion['growth_set']['ambiguous_at_top2_boundary']} 道 Top-2 边界并列。简单等权 RRF 是折中，不是整体提升。
- 结果支持继续验证“按任务路由”的方向：单值引用优先 BGE，增长来源集合评估微调 BERT；本轮诊断集不足以把这一路由直接定为生产策略，需在新的人工核验集上复核。

## 融合相对单模的逐题变化

| 对照 | 任务 | 融合胜 | 持平 | 融合负 |
|---|---|---:|---:|---:|
'''
    for baseline_name, baseline_label in (("bge", "BGE"), ("bert", "年报修正版 BERT")):
        for task, task_label in (("quote_current", "本期引用"), ("quote_prior", "上期引用"), ("growth_set", "增长来源集合")):
            row = output["fusion_vs_single_model"][baseline_name][task]
            report += f"| 融合 vs {baseline_label} | {task_label} | {row['fusion_wins']} | {row['ties']} | {row['fusion_losses']} |\n"
    agreement = output["model_top_prediction_agreement"]
    report += f'''

两单模在单值题 Top-1 上一致率为 {agreement['single_value_top1_agreement']:.2%}（{quote_agree}/{quote_n}），在增长题 Top-2 来源集合上一致率为 {agreement['growth_top2_set_agreement']:.2%}（{growth_agree}/{growth_n}）。融合后候选顺序随机打乱，预测集合变化 {changed} 道，顺序一致率 {output['candidate_order_invariance']['prediction_set_consistency']:.2%}。

## 时间与限制

- 本次测得 BGE 推理 {bge_seconds:.2f} 秒、BERT 推理 {bert_seconds:.2f} 秒，双模型推理合计 {bge_seconds + bert_seconds:.2f} 秒。BGE 使用 {bge_device}、BERT 使用 {bert_device}（RTX 5060），分别适用于当前部署形态；此数据不能解释为同硬件速度对比。
- 两个模型都对本诊断集的候选打分，因此本实验测的是**双模型重排融合**，不等同于线上完整文档端到端耗时或 API 成本。
- 诊断集由 PDF 解析及程序规则生成弱标签，非人工金标准；融合效果不能作为正式泛化准确率承诺。
- 融合不是天然优于单模。是否产品化应看目标任务：错误关联成本高时仍应阈值拒答；没有人工语义集验证前，不应仅凭本轮结果改变默认路由。

复现：`.train-cuda-venv\\Scripts\\python.exe 答辩评测\\evaluate_bge_bert_ensemble.py`
'''
    OUT_REPORT.write_text(report, encoding="utf-8")
    print(json.dumps({
        "metrics": str(OUT_JSON),
        "report": str(OUT_REPORT),
        "chart": str(OUT_CHART),
        "rows": len(rows),
        "claims": len(grouped),
        "fusion": fused_metrics["by_task"],
        "elapsed_seconds": output["elapsed_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
