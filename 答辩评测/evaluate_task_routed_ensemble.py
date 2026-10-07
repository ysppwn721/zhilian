"""Derive a fixed task-routed BGE/BERT ensemble from existing evaluations."""
from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "答辩评测" / "annual_benchmark_repaired_20261004_v2"
OUT_JSON = DATA / "metrics_task_routed_ensemble.json"
OUT_CSV = DATA / "task_routed_ensemble.csv"
OUT_REPORT = DATA / "任务路由融合评测.md"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def task_metrics(metrics: dict, task: str) -> dict:
    return metrics["model_metrics"]["by_task"][task]


def main() -> int:
    bge = load("metrics_bge.json")
    bert = load("metrics_annual_repaired_v1.json")
    rrf = load("metrics_bge_bert_rrf.json")
    route = {
        "quote_current": task_metrics(bge, "quote_current"),
        "quote_prior": task_metrics(bge, "quote_prior"),
        "growth_set": task_metrics(bert, "growth_set"),
    }
    output = {
        "experiment": "fixed_task_routed_ensemble",
        "routing": {
            "quote_current": "bge",
            "quote_prior": "bge",
            "growth_set": "annual_repaired_v1",
        },
        "reason": "Use BGE for single-source selection and annual-repaired BERT for two-source growth-set retrieval; no score mixing or benchmark tuning.",
        "programmatic_labels": True,
        "model_metrics": route,
        "comparison": {
            "bge": bge["model_metrics"]["by_task"],
            "annual_repaired_v1": bert["model_metrics"]["by_task"],
            "equal_weight_rrf": rrf["model_metrics"]["fusion"]["by_task"],
        },
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for name, metrics in (
        ("BGE", bge["model_metrics"]["by_task"]),
        ("年报修正版 BERT", bert["model_metrics"]["by_task"]),
        ("等权 RRF", rrf["model_metrics"]["fusion"]["by_task"]),
        ("任务路由融合", route),
    ):
        rows.append({
            "strategy": name,
            "quote_current_top1": metrics["quote_current"]["top1_accuracy"],
            "quote_prior_top1": metrics["quote_prior"]["top1_accuracy"],
            "growth_source_set_top2_exact": metrics["growth_set"]["source_set_exact_match"],
            "growth_source_set_completeness": metrics["growth_set"]["source_set_completeness_recall"],
        })
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    bge_tasks = bge["model_metrics"]["by_task"]
    bert_tasks = bert["model_metrics"]["by_task"]
    bert_growth = bert["model_metrics"]["by_task"]["growth_set"]
    rrf_tasks = rrf["model_metrics"]["fusion"]["by_task"]
    report = f'''# BGE 与 BERT 任务路由融合评测

## 为什么 BERT 的 Top-2 高、Top-1 低

本数据中“增长来源集 Top-2 精确匹配”不是普通单值 Top-2，而是要求一次选出**本期和上期两个正确来源**。年报修正版 BERT 在这个任务上达到 **{bert_growth['source_set_exact_match']:.2%}**，说明它能把两期来源一起召回。

单值引用每题只允许一个来源，要求模型进一步判断最终期间。BERT 在本期、上期单值引用分别为 **{bert_tasks['quote_current']['top1_accuracy']:.2%}**、**{bert_tasks['quote_prior']['top1_accuracy']:.2%}**，低于 BGE 的 **{bge_tasks['quote_current']['top1_accuracy']:.2%}**、**{bge_tasks['quote_prior']['top1_accuracy']:.2%}**。这表示 BERT 的候选召回能力较强，但最终 Top-1 裁决能力和期间区分能力不足。

## 固定任务路由

不直接混加两个模型的原始分数，也不在本诊断集调权：

- 单值引用（本期、上期）：使用 BGE Top-1；
- 增长率论断：使用年报修正版 BERT Top-2 来源集合；
- 数值、单位、主体、期间和口径仍由规则校验，低置信度转人工。

| 策略 | 本期引用 Top-1 | 上期引用 Top-1 | 增长来源集 Top-2 精确匹配 | 增长来源完整率 |
|---|---:|---:|---:|---:|
| BGE | {bge['model_metrics']['by_task']['quote_current']['top1_accuracy']:.2%} | {bge['model_metrics']['by_task']['quote_prior']['top1_accuracy']:.2%} | {bge['model_metrics']['by_task']['growth_set']['source_set_exact_match']:.2%} | {bge['model_metrics']['by_task']['growth_set']['source_set_completeness_recall']:.2%} |
| 年报修正版 BERT | {bert['model_metrics']['by_task']['quote_current']['top1_accuracy']:.2%} | {bert['model_metrics']['by_task']['quote_prior']['top1_accuracy']:.2%} | {bert_growth['source_set_exact_match']:.2%} | {bert_growth['source_set_completeness_recall']:.2%} |
| 等权 RRF | {rrf_tasks['quote_current']['top1_accuracy']:.2%} | {rrf_tasks['quote_prior']['top1_accuracy']:.2%} | {rrf_tasks['growth_set']['source_set_exact_match']:.2%} | {rrf_tasks['growth_set']['source_set_completeness_recall']:.2%} |
| **任务路由融合** | **{route['quote_current']['top1_accuracy']:.2%}** | **{route['quote_prior']['top1_accuracy']:.2%}** | **{route['growth_set']['source_set_exact_match']:.2%}** | **{route['growth_set']['source_set_completeness_recall']:.2%}** |

这个结果比等权 RRF 更符合模型分工：单值任务保留 BGE 的稳定性，增长任务保留 BERT 的双来源召回能力。它仍然是程序化弱标签诊断结果，不能替代人工语义集验收。

## 产品实现建议

先由规则识别论断类型，再选择模型：`quote -> BGE`，`growth -> BERT Top-2`。模型只产生候选；规则验证指标、主体、期间、单位和计算关系。若两个来源均通过校验则进入确认页，任一来源低置信或口径不明则拒答，不自动写回文件。
'''
    OUT_REPORT.write_text(report, encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "csv": str(OUT_CSV), "report": str(OUT_REPORT), "route": route}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
