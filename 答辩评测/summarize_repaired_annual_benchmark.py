"""Create a concise comparison report and PPT-ready raster chart."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "annual_benchmark_repaired_20261004_v2"
sys.path.insert(0, str(ROOT))
from evaluate_repaired_annual_benchmark import deterministic_baselines, read_rows
MODELS = ("bge", "bert_v1", "bert_v2", "annual_weak_v1", "annual_repaired_v1")
MODEL_LABELS = {
    "bge": "BGE Reranker v2-m3",
    "bert_v1": "BERT v1",
    "bert_v2": "BERT v2",
    "annual_weak_v1": "年报弱监督 BERT",
    "annual_repaired_v1": "修正版弱监督 BERT",
}


def load_results() -> tuple[dict, list[dict]]:
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    baselines = deterministic_baselines(read_rows(DATA_DIR / "benchmark.jsonl"))
    results = []
    for model in MODELS:
        path = DATA_DIR / f"metrics_{model}.json"
        if not path.is_file():
            raise SystemExit(f"Missing model result: {path}")
        result = json.loads(path.read_text(encoding="utf-8"))
        result["baselines"] = baselines
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tasks = result["model_metrics"]["by_task"]
        quote_current = tasks["quote_current"]["top1_accuracy"]
        quote_prior = tasks["quote_prior"]["top1_accuracy"]
        growth = tasks["growth_set"]
        results.append({
            "model": model,
            "model_label": MODEL_LABELS[model],
            "device": result["device"],
            "quote_current_top1": quote_current,
            "quote_prior_top1": quote_prior,
            "quote_mean_top1": (quote_current + quote_prior) / 2,
            "growth_source_set_exact_match": growth["source_set_exact_match"],
            "growth_source_completeness_recall": growth["source_set_completeness_recall"],
            "claims": result["claims"],
            "candidate_pairs": result["rows"],
            "elapsed_seconds": result["elapsed_seconds_evaluation"],
            "candidate_order_sample_claims": result["candidate_order_invariance"]["rescore_sample"]["claims"],
            "candidate_order_sample_consistency": result["candidate_order_invariance"]["rescore_sample"]["prediction_set_consistency"],
            "_baselines": baselines,
        })
    return manifest, results


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/msyh.ttc")]
        if bold else [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf")]
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def create_chart(results: list[dict]) -> None:
    width, height = 1800, 1050
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    title_font = font(46, True)
    subtitle_font = font(26)
    label_font = font(23, True)
    tick_font = font(20)
    small_font = font(18)

    draw.text((105, 55), "修正版年报候选排序诊断", font=title_font, fill="#18212B")
    draw.text(
        (108, 120),
        "同主体同期间异指标干扰 · 当前/上期引用分开统计 · 增长题按双来源集合评测",
        font=subtitle_font,
        fill="#53616F",
    )

    colors = ["#16877B", "#D98236", "#5968B0"]
    legend = ["本期引用 Top-1", "上期引用 Top-1", "增长来源集 Top-2 精确匹配"]
    lx, ly = 112, 190
    for color, label in zip(colors, legend):
        draw.rounded_rectangle((lx, ly + 3, lx + 26, ly + 27), radius=4, fill=color)
        draw.text((lx + 38, ly), label, font=small_font, fill="#34404B")
        lx += 365

    chart_left, chart_top = 170, 275
    chart_width, chart_height = 1500, 555
    chart_bottom = chart_top + chart_height
    for tick in range(0, 101, 20):
        y = chart_bottom - chart_height * tick / 100
        draw.line((chart_left, y, chart_left + chart_width, y), fill="#DCE2E8", width=2)
        draw.text((chart_left - 66, y - 14), f"{tick}%", font=tick_font, fill="#65717C")

    group_width = chart_width / len(results)
    bar_width, bar_gap = 78, 28
    for group_index, result in enumerate(results):
        values = [
            result["quote_current_top1"],
            result["quote_prior_top1"],
            result["growth_source_set_exact_match"],
        ]
        block_width = len(values) * bar_width + (len(values) - 1) * bar_gap
        start_x = chart_left + group_index * group_width + (group_width - block_width) / 2
        for value_index, (value, color) in enumerate(zip(values, colors)):
            x = start_x + value_index * (bar_width + bar_gap)
            bar_height = chart_height * value
            y = chart_bottom - bar_height
            draw.rounded_rectangle((x, y, x + bar_width, chart_bottom), radius=7, fill=color)
            text = f"{value:.1%}"
            bounds = draw.textbbox((0, 0), text, font=label_font)
            draw.text((x + (bar_width - (bounds[2] - bounds[0])) / 2, y - 36), text,
                      font=label_font, fill="#26323C")
        model_name = result["model_label"]
        bounds = draw.textbbox((0, 0), model_name, font=label_font)
        x = chart_left + group_index * group_width + (group_width - (bounds[2] - bounds[0])) / 2
        draw.text((x, chart_bottom + 24), model_name, font=label_font, fill="#26323C")

    draw.line((chart_left, chart_bottom, chart_left + chart_width, chart_bottom), fill="#8D99A4", width=2)
    draw.text((110, 900), "标签：PDF解析与规则生成的程序化弱标签，非人工金标准。", font=small_font, fill="#34404B")
    draw.text((110, 933), "设备：BGE 为 CPU ONNX；BERT 为 RTX 5060 CUDA。横向比较准确率，不比较本次耗时。", font=small_font, fill="#34404B")
    draw.text((110, 966), "增长集合精确匹配要求 Top-2 同时选中本期与上期来源；不是普通 Top-1。", font=small_font, fill="#34404B")
    image.save(DATA_DIR / "model_comparison.png", optimize=True)


def write_csv(results: list[dict]) -> None:
    path = DATA_DIR / "model_comparison.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        export_results = [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in results
        ]
        writer = csv.DictWriter(stream, fieldnames=list(export_results[0]))
        writer.writeheader()
        writer.writerows(export_results)


def write_report(manifest: dict, results: list[dict]) -> None:
    model_rows = []
    for row in results:
        model_rows.append(
            f"| {row['model_label']} | {row['quote_current_top1']:.2%} | "
            f"{row['quote_prior_top1']:.2%} | {row['quote_mean_top1']:.2%} | "
            f"{row['growth_source_set_exact_match']:.2%} | "
            f"{row['growth_source_completeness_recall']:.2%} | {row['device']} |"
        )
    baselines = results[0]["_baselines"]
    quote_rule = baselines["quote_current_prior"]["literal_metric_and_period_rule"]
    alias_quote_rule = baselines["quote_current_prior"]["controlled_alias_lexicon_and_period_rule"]
    growth_rule = baselines["growth_set"]["literal_metric_set_rule"]
    alias_growth_rule = baselines["growth_set"]["controlled_alias_lexicon_set_rule"]
    audit = manifest["audit"]
    report = f'''# 修正版年报候选排序评测

## 实验范围

- 数据：{manifest['reports']} 份年报、{manifest['companies']} 家公司、{manifest['claims']} 道论断、{manifest['rows']:,} 个论断—事实候选对。
- 单值引用：本期 {manifest['period_positive_claims']['quote_current']} 道、上期 {manifest['period_positive_claims']['quote_prior']} 道，正例数量配平；每题 9 个候选、1 个正例。
- 增长引用：{manifest['source_set_growth_claims']} 道；每题从 16 个候选中选两个来源，按本期+上期来源集合精确匹配和完整率计分。
- 候选组成：同主体、同来源报告、同期间异指标干扰 {audit['candidate_role_counts']['same_subject_same_period_other_metric']:,} 条；同指标错期间干扰 {audit['candidate_role_counts']['same_metric_wrong_period']:,} 条；正例 {audit['candidate_role_counts']['gold_source']:,} 条。
- 构造审计：{audit['all_claims_have_same_period_other_metric_distractor']} 每题含同期间异指标候选；{audit['all_labels_match_gold_fact_ids']} 标签与来源集合一致；首位正例率 {manifest['position_0_positive_rate']:.2%}。

这是一份由 PDF 解析和程序规则生成的弱标签诊断集，不是人工标注金标准，也不是新增的独立外部语料。源年报中报告口径和解析单位仍可能存在误差；受控同义改写只代表本次配置的别名，不代表完整真实语言分布。

## 模型对照

| 模型 | 本期引用 Top-1 | 上期引用 Top-1 | 两类引用均值 | 增长来源集 Top-2 精确匹配 | 增长来源完整率 | 推理设备 |
|---|---:|---:|---:|---:|---:|---|
{chr(10).join(model_rows)}

图片：`model_comparison.png`。可编辑数据：`model_comparison.csv`。单值引用 Top-1 和增长来源集合精确匹配是不同任务指标，不应合并成一个“总准确率”。

## 简单规则基线

- 单值题按最长字面指标词和期间筛候选：回答覆盖率 {quote_rule['coverage']:.2%}（{quote_rule['answered']}/{quote_rule['claims']}）；对已回答题的 Top-1 正确率 {quote_rule['top1_accuracy_on_answered']:.2%}；折算到全部单值题正确率 {quote_rule['correct_over_all_claims']:.2%}。显式加入本诊断集使用的 {alias_quote_rule['lexicon_entries']} 条别名词典后，覆盖率为 {alias_quote_rule['coverage']:.2%}、全部题正确率为 {alias_quote_rule['correct_over_all_claims']:.2%}。这只是固定词表规则对照，不代表开放式语义能力。
- 增长题按最长字面指标词筛来源集合：回答覆盖率 {growth_rule['coverage']:.2%}（{growth_rule['answered']}/{growth_rule['claims']}）；已回答题来源集合精确匹配 {growth_rule['source_set_exact_match_on_answered']:.2%}；折算到全部增长题为 {growth_rule['exact_over_all_claims']:.2%}。加入相同的显式别名词典后，覆盖率为 {alias_growth_rule['coverage']:.2%}，全部题来源集合精确匹配为 {alias_growth_rule['exact_over_all_claims']:.2%}。
- 只按目标期间筛选，平均留下 {baselines['quote_current_prior']['period_only_candidate_count_mean']:.1f} 个候选；期间匹配本身无法消歧。随机猜 Top-1 的期望正确率为 {baselines['quote_current_prior']['random_choice_expected_accuracy']:.2%}。

## 结果解读

- BGE 在本期与上期单值引用上较均衡，分别为 {results[0]['quote_current_top1']:.2%} 和 {results[0]['quote_prior_top1']:.2%}；但增长题来源集合精确匹配为 {results[0]['growth_source_set_exact_match']:.2%}，完整率为 {results[0]['growth_source_completeness_recall']:.2%}。能识别单个来源，不等于能完整组装增长率的两期来源。
- 旧 BERT v1、v2 的增长集合精确匹配分别为 {results[1]['growth_source_set_exact_match']:.2%}、{results[2]['growth_source_set_exact_match']:.2%}，但两者在本期/上期单值引用上的表现明显偏斜。当前年报弱监督 BERT 的增长集合精确匹配为 {results[3]['growth_source_set_exact_match']:.2%}，没有显示出可接受的增量。
- 本轮没有重训，也没有把任何候选模型接入生产默认路由。当前证据不支持“新训练模型优于 BGE”，更支持按任务区分模型作用，并在低置信/多来源场景保留程序校验与拒答。

## 顺序稳定性与实现修正

- 全量 1,275 道题使用同一批逐对模型分数重新打乱候选并执行排序，预测集合变化 0。
- 另对每个任务类型抽取 16 道题（共 48 道）在打乱后重新推理。四个模型的分数和预测集合均保持一致。
- 首轮 BGE 复测曾观察到同一输入对因变长批次 padding 长度而产生大幅分数变化；检查后发现评测器错误地将 `<pad>` 设成了 ID 0（模型要求 ID 1），且批次 padding 长度依赖候选顺序。该轮结果已弃用。修正版使用模型的 pad ID，并按输入长度、claim ID、fact ID 确定批次；修正版重测的抽样顺序一致性为 100%。

## 可比性与限制

- BGE 使用 CPU ONNX；三个 BERT 使用 NVIDIA RTX 5060 CUDA。报告里的运行秒数包含加载和抽样复算，设备不同，不作为延迟横向结论。
- 36 份报告来自 19 家公司，报告文件不是独立统计样本。年报弱监督训练集的 92 家训练公司与本基准 19 家公司交集为 0；冻结 JSONL 文件哈希前后未变。本数据仍是原 holdout 报告的诊断性重构，不应宣称为全新盲测集。
- 只有经人工核验的独立语义集，才能支撑正式泛化准确率结论。当前模型分数用于发现构造偏差和模型适用边界，不应用作产品准确率承诺。

## 复现

```powershell
.venv\\Scripts\\python.exe 答辩评测\\build_repaired_annual_benchmark.py --out 答辩评测\\annual_benchmark_repaired_20261004_v2
.venv\\Scripts\\python.exe 答辩评测\\evaluate_repaired_annual_benchmark.py --model bge --device cpu --batch-size 64
.train-cuda-venv\\Scripts\\python.exe 答辩评测\\evaluate_repaired_annual_benchmark.py --model bert_v1 --device cuda --batch-size 64
.train-cuda-venv\\Scripts\\python.exe 答辩评测\\evaluate_repaired_annual_benchmark.py --model bert_v2 --device cuda --batch-size 64
.train-cuda-venv\\Scripts\\python.exe 答辩评测\\evaluate_repaired_annual_benchmark.py --model annual_weak_v1 --device cuda --batch-size 64
```
'''
    (DATA_DIR / "评测报告.md").write_text(report, encoding="utf-8")


def main() -> int:
    manifest, results = load_results()
    write_csv(results)
    create_chart(results)
    write_report(manifest, results)
    print(json.dumps({"models": len(results), "claims": manifest["claims"], "output": str(DATA_DIR)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
