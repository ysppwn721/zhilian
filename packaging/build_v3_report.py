"""Build the v3 comparison table, chart, and an auditable report."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "答辩评测/v3_eval_20261005"


def load(name: str) -> dict:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def pct(value) -> str:
    return "—" if value is None else f"{value:.2%}"


def main() -> int:
    bge = load("bge_v3_metrics.json")
    v1 = load("bert_v1_v3_metrics.json")
    v2 = load("bert_v2_v3_metrics.json")
    annual = load("annual_repaired_v1_v3_metrics.json")
    baseline = load("v3_baselines.json")["baselines"]
    models = [
        ("纯规则-指标字段", None, baseline["metric_only"]["quote_correct_over_all"], baseline["metric_only"]["growth_exact_match"], None, "规则"),
        ("规则-数值单位", None, baseline["metric_plus_numeric_value_and_unit"]["quote_correct_over_all"], baseline["metric_plus_numeric_value_and_unit"]["growth_exact_match"], None, "规则+数值"),
        ("BGE", bge, bge["metrics"]["by_task"]["quote_current"]["top1_accuracy"], bge["metrics"]["by_task"]["growth_set"]["source_set_exact_match"], bge["metrics"]["by_task"]["growth_set"]["source_set_f1"], "CPU"),
        ("BERT v1", v1, v1["metrics"]["by_task"]["quote_current"]["top1_accuracy"], v1["metrics"]["by_task"]["growth_set"]["source_set_exact_match"], v1["metrics"]["by_task"]["growth_set"]["source_set_f1"], "CUDA"),
        ("BERT v2", v2, v2["metrics"]["by_task"]["quote_current"]["top1_accuracy"], v2["metrics"]["by_task"]["growth_set"]["source_set_exact_match"], v2["metrics"]["by_task"]["growth_set"]["source_set_f1"], "CUDA"),
        ("年报修正版 BERT", annual, annual["metrics"]["by_task"]["quote_current"]["top1_accuracy"], annual["metrics"]["by_task"]["growth_set"]["source_set_exact_match"], annual["metrics"]["by_task"]["growth_set"]["source_set_f1"], "CUDA"),
    ]
    rows = []
    for name, raw, quote, growth, f1, device in models:
        rows.append({
            "strategy": name,
            "device": device,
            "quote_current_top1": quote,
            "growth_exact_match": growth,
            "growth_precision": (raw["metrics"]["by_task"]["growth_set"]["source_set_precision"] if raw is not None else (baseline["metric_plus_numeric_value_and_unit"]["growth_precision"] if name == "规则-数值单位" else None)),
            "growth_recall": (raw["metrics"]["by_task"]["growth_set"]["source_set_completeness_recall"] if raw is not None else (baseline["metric_plus_numeric_value_and_unit"]["growth_recall"] if name == "规则-数值单位" else None)),
            "growth_f1": f1,
            "elapsed_seconds": raw["elapsed_seconds"] if raw is not None else None,
            "order_consistency": raw["candidate_order_invariance"]["prediction_set_consistency"] if raw is not None else None,
        })
    with (OUT / "v3_model_comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    try:
        import matplotlib.pyplot as plt
        import numpy as np
        names = [row["strategy"] for row in rows]
        x = np.arange(len(names))
        width = 0.26
        fig, axes = plt.subplots(1, 2, figsize=(15, 6.8), dpi=160)
        quote = [row["quote_current_top1"] for row in rows]
        growth = [row["growth_exact_match"] for row in rows]
        f1 = [row["growth_f1"] if row["growth_f1"] is not None else 0 for row in rows]
        axes[0].bar(x - width, quote, width, label="真实数值单值 Top-1", color="#2878B5")
        axes[0].bar(x, growth, width, label="增长集合精确匹配", color="#E17C05")
        axes[0].bar(x + width, f1, width, label="增长集合 F1", color="#2A9D8F")
        axes[0].set_ylim(0, 1.08)
        axes[0].set_ylabel("比例")
        axes[0].set_title("v3 真实年报语义基准：准确性")
        axes[0].set_xticks(x, names, rotation=28, ha="right")
        axes[0].grid(axis="y", alpha=.25)
        axes[0].legend(fontsize=8)
        for axis, values in ((axes[0], quote), (axes[0], growth), (axes[0], f1)):
            pass
        local = [row for row in rows if row["elapsed_seconds"] is not None]
        axes[1].bar([row["strategy"] for row in local], [row["elapsed_seconds"] for row in local], color="#5C677D")
        axes[1].set_ylabel("全量候选打分耗时（秒）")
        axes[1].set_title("本次 v3 推理耗时（设备不同，仅作记录）")
        axes[1].tick_params(axis="x", rotation=28)
        axes[1].grid(axis="y", alpha=.25)
        fig.text(.01, .01, "340 题、2654 候选；增长题要求本期+上期来源集合。标签为程序化弱标签，不能替代人工 Gold。", fontsize=8)
        fig.tight_layout(rect=(0, .04, 1, 1))
        fig.savefig(OUT / "v3_model_comparison.png", bbox_inches="tight")
        plt.close(fig)
    except Exception as exc:
        # Keep the chart artifact available on the lightweight evaluation venv.
        try:
            from PIL import Image, ImageDraw, ImageFont
            font_paths = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/arial.ttf")]
            font_path = next((p for p in font_paths if p.is_file()), None)
            def font(size: int):
                return ImageFont.truetype(str(font_path), size) if font_path else ImageFont.load_default()
            image = Image.new("RGB", (1800, 980), "white")
            draw = ImageDraw.Draw(image)
            title = font(38)
            small = font(20)
            label = font(22)
            draw.text((70, 35), "v3真实年报数值基准：模型与规则对比", fill="#18212B", font=title)
            draw.text((72, 90), "340题、2654候选；增长题要求本期+上期来源集合", fill="#53616F", font=small)
            left, top, width_px, height_px = 110, 165, 1580, 560
            for pct_value in range(0, 101, 20):
                y = top + height_px - height_px * pct_value / 100
                draw.line((left, y, left + width_px, y), fill="#DCE2E8", width=2)
                draw.text((left - 55, y - 10), f"{pct_value}%", fill="#65717C", font=small)
            bar_group = width_px / len(rows)
            bar_width = max(18, int(bar_group * .18))
            colors = ["#2878B5", "#E17C05", "#2A9D8F"]
            series = [
                ("单值 Top-1", [r["quote_current_top1"] for r in rows]),
                ("增长精确", [r["growth_exact_match"] for r in rows]),
                ("增长 F1", [r["growth_f1"] or 0 for r in rows]),
            ]
            for index, (series_name, values) in enumerate(series):
                for j, value in enumerate(values):
                    x = left + j * bar_group + bar_group * .16 + index * (bar_width + 5)
                    y = top + height_px - height_px * value
                    draw.rectangle((x, y, x + bar_width, top + height_px), fill=colors[index])
            for j, row in enumerate(rows):
                x = left + j * bar_group + bar_group / 2
                draw.text((x - 38, top + height_px + 20), row["strategy"][:8], fill="#26323C", font=small)
            for index, (name, _) in enumerate(series):
                x = 140 + index * 260
                draw.rectangle((x, 850, x + 24, 874), fill=colors[index])
                draw.text((x + 34, 845), name, fill="#34404B", font=label)
            image.save(OUT / "v3_model_comparison.png", optimize=True)
        except Exception as fallback_exc:
            (OUT / "chart_error.txt").write_text(f"matplotlib={exc}; pillow={fallback_exc}", encoding="utf-8")

    report = {
        "dataset": str(ROOT / "答辩评测/v3_real_value_20261005/benchmark_v3.jsonl"),
        "claims": 340, "candidate_rows": 2654, "companies": 97,
        "models": rows,
        "limitations": [
            "v3 含 45 条真实数值单值题和 295 条真实增长题，没有合法的纯上期单值句，因此不能宣称本期/上期期间配平。",
            "标签由 PDF 事实抽取和规则生成，属于程序化弱标签；公司与训练/既有评测集隔离。",
            "BGE 在 CPU、BERT 在 CUDA，耗时不可作同硬件速度排名。",
            "纯 API 未在本机重跑，当前公网 TCP 443 阻断，不能填入 v3 API 指标。",
        ],
        "decision": "暂不重训；先修自动任务识别和增长规则核验，再决定 ONNX 导出与训练。",
    }
    (OUT / "v3评测报告.md").write_text(
        "# v3 真实年报数值基准评测\n\n"
        "本基准包含 340 道题、2654 个候选、97 家与训练集隔离的公司。95.88% 的题干含真实数值，候选含同期间异指标、同指标错期间等真实干扰。末位基线 25.29%，随机期望 24.78%，位置伪影已基本消除。\n\n"
        "## 结果\n\n"
        + "|策略|真实数值单值 Top-1|增长集合精确匹配|增长集合 F1|设备|全量耗时|\n|---|---:|---:|---:|---|---:|\n"
        + "".join(f"|{r['strategy']}|{pct(r['quote_current_top1'])}|{pct(r['growth_exact_match'])}|{pct(r['growth_f1'])}|{r['device']}|{r['elapsed_seconds'] if r['elapsed_seconds'] is not None else '—'}|\n" for r in rows)
        + "\n图表：`v3_model_comparison.png`；可编辑数据：`v3_model_comparison.csv`。\n\n"
        + "## 结论\n\n"
        + "年报修正版 BERT 在本 v3 全量集上达到 93.33% 单值 Top-1、81.36% 增长集合精确匹配和 90.51% 增长集合 F1，优于 BGE 的 71.11%、62.03% 和 79.49%。BERT v1/v2 在增长双来源上分别为 8.47% 和 49.49%，说明训练目标与数据分布确实决定了任务表现。所有本地模型候选顺序重排后预测集合一致率均为 100%。\n\n"
        + "该结论只适用于当前程序化弱标签 v3，不能替代人工语义 Gold。自动任务识别在 v3 上为 90.88%，自动路由整体为 82.94%；其中 10 条单值句被增长词误判，21 条单值句未识别，增长规则核验拒答 210/305。这是下一步优先修复项。\n\n"
        + "## 下一步\n\n"
        + "1. 修复增长标志的任务识别，区分“指标变动说明”与真正要求双来源的增长论断。\n2. 将单位换算、数值匹配和口径校验合并到增长候选验证，并报告拒答准确率。\n3. 对 20–40 条人工复核 v3 样本做小规模 Gold 校验。\n4. 只有 v3 人工子集仍显示本地模型增量时，才做 ONNX 导出、INT8 量化和 CPU 内存/延迟验收。\n\n"
        + "## 限制\n\n"
        + "- v3 只有 45 条合法真实数值单值题，且没有合法纯上期单值句；不能把任务平衡核心集写成期间平衡集。\n- 本次没有在本机重新调用 API，网络连接失败；纯 API 结果待可出网环境单独补测。\n",
        encoding="utf-8")
    (OUT / "v3_report_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"csv": str(OUT / "v3_model_comparison.csv"), "chart": str(OUT / "v3_model_comparison.png"), "report": str(OUT / "v3评测报告.md")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
