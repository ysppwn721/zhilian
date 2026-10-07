"""Build separate effect, latency, and upstream-token-cost charts.

The only fair pure-API/routed comparison currently available is the completed
72-claim semantic-rewrite replay.  This script keeps that historical replay
separate from the current 37-claim human-Gold subset, where pure API has not
been measured in the same environment.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "答辩评测"
OUT = EVAL / "api_vs_three_route_20261005"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _font(size: int):
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _pillow_bar_chart(path: Path, title: str, subtitle: str, labels: list[str],
                      series: list[tuple[str, list[float], str]], y_label: str,
                      percent: bool = False, decimals: int = 3) -> None:
    from PIL import Image, ImageDraw
    width, height = 1800, 1050
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, small_font = _font(38), _font(22), _font(20)
    draw.text((70, 35), title, fill="#172033", font=title_font)
    draw.text((72, 92), subtitle, fill="#536174", font=small_font)
    left, top, plot_w, plot_h = 150, 165, 1530, 650
    max_value = max(max(values) for _, values, _ in series) or 1.0
    if percent:
        max_value = 1.0
    for tick in range(6):
        value = max_value * tick / 5
        y = top + plot_h - plot_h * value / max_value
        draw.line((left, y, left + plot_w, y), fill="#DCE2E8", width=2)
        draw.text((left - 90, y - 11), f"{value:.0%}" if percent else f"{value:.{decimals}f}", fill="#65717C", font=small_font)
    group_w = plot_w / len(labels)
    bar_w = max(22, int(group_w * 0.18))
    for si, (name, values, color) in enumerate(series):
        for i, value in enumerate(values):
            x = left + i * group_w + group_w / 2 + (si - (len(series) - 1) / 2) * (bar_w + 8) - bar_w / 2
            y = top + plot_h - plot_h * value / max_value
            draw.rectangle((x, y, x + bar_w, top + plot_h), fill=color)
            text = f"{value:.1%}" if percent else f"{value:.{decimals + 3}f}"
            draw.text((x + bar_w / 2, max(top + 8, y - 31)), text, fill="#26323C", font=small_font, anchor="mm")
    for i, label in enumerate(labels):
        draw.text((left + i * group_w + group_w / 2, top + plot_h + 35), label,
                  fill="#26323C", font=label_font, anchor="ma")
    legend_x = 170
    for name, _, color in series:
        draw.rectangle((legend_x, 905, legend_x + 24, 929), fill=color)
        draw.text((legend_x + 34, 897), name, fill="#34404B", font=label_font)
        legend_x += 250
    draw.text((70, 995), y_label, fill="#536174", font=small_font)
    image.save(path, optimize=True)


def chart_effect(report: dict) -> None:
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ModuleNotFoundError:
        rows = report["metrics"]
        labels = ["正确关联率", "覆盖率", "拒答率", "错误关联率"]
        series = [
            ("纯 API", [rows[0]["top1_accuracy"], rows[0]["coverage"], rows[0]["abstain_rate"], rows[0]["error_association_rate"]], "#7B8794"),
            ("三层路由", [rows[1]["top1_accuracy"], rows[1]["coverage"], rows[1]["abstain_rate"], rows[1]["error_association_rate"]], "#1976A3"),
        ]
        _pillow_bar_chart(OUT / "效果对比_纯API_vs_三层路由.png", "纯 API 与三层路由：效果对比",
                          "72 条同集程序化语义改写回放；三层路由按规则、本地模型、API 兜底分流",
                          labels, series, "比例", percent=True)
        return

    rows = report["metrics"]
    labels = ["正确关联率", "覆盖率", "拒答率", "错误关联率"]
    values = [
        [rows[0]["top1_accuracy"], rows[0]["coverage"], rows[0]["abstain_rate"], rows[0]["error_association_rate"]],
        [rows[1]["top1_accuracy"], rows[1]["coverage"], rows[1]["abstain_rate"], rows[1]["error_association_rate"]],
    ]
    x = np.arange(len(labels))
    width = 0.35
    fig, ax = plt.subplots(figsize=(11.5, 6.8), dpi=180)
    bars_a = ax.bar(x - width / 2, values[0], width, label="纯 API", color="#7B8794")
    bars_b = ax.bar(x + width / 2, values[1], width, label="三层路由", color="#1976A3")
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("比例")
    ax.set_xticks(x, labels)
    ax.set_title("纯 API 与三层路由：效果对比")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, ncol=2, loc="upper right")
    for bars in (bars_a, bars_b):
        for bar in bars:
            value = bar.get_height()
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.018, f"{value:.1%}",
                    ha="center", va="bottom", fontsize=9)
    route = report["route_counts"]
    ax.text(0.01, -0.18,
            f"同一评测集：72 条；三层路由=规则 {route['rule_unique']} + 本地模型 {route['local']} + API 兜底 {route['api']}。"
            " 标签为程序化语义改写评测，不是人工行业真值。",
            transform=ax.transAxes, fontsize=8.5, color="#4B5563")
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    fig.savefig(OUT / "效果对比_纯API_vs_三层路由.png", bbox_inches="tight")
    plt.close(fig)


def chart_latency(report: dict) -> None:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        latency = report["latency"]
        _pillow_bar_chart(OUT / "时间对比_纯API_vs_三层路由.png", "纯 API 与三层路由：时间对比",
                          "纯 API 为真实网络回放；三层路由仅包含规则+本地模型，API 兜底未联网实测",
                          ["纯 API\n网络回放", "三层路由\n规则+本地前置"],
                          [("耗时", [latency["pure_api_seconds"], latency["routed_rule_local_seconds"]], "#1976A3")],
                          "秒")
        return

    latency = report["latency"]
    pure = latency["pure_api_seconds"]
    front = latency["routed_rule_local_seconds"]
    fig, ax = plt.subplots(figsize=(10.5, 6.2), dpi=180)
    bars = ax.bar(["纯 API\n网络回放", "三层路由\n规则+本地前置"], [pure, front],
                  color=["#7B8794", "#1976A3"], width=0.52)
    ax.set_ylabel("耗时（秒）")
    ax.set_title("纯 API 与三层路由：时间对比")
    ax.grid(axis="y", alpha=0.25)
    for bar, value in zip(bars, [pure, front]):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.18, f"{value:.3f}s",
                ha="center", va="bottom", fontsize=11)
    ax.text(0.02, -0.19,
            "纯 API 为 72 条真实 API 回放；三层路由柱只包含规则+本地模型实测，"
            "14 条 API 兜底未在本机联网，因此不能宣称为端到端总耗时。",
            transform=ax.transAxes, fontsize=8.5, color="#4B5563")
    fig.tight_layout(rect=(0, 0.11, 1, 1))
    fig.savefig(OUT / "时间对比_纯API_vs_三层路由.png", bbox_inches="tight")
    plt.close(fig)


def chart_cost(report: dict) -> None:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        rows = report["cost"]
        _pillow_bar_chart(OUT / "费用对比_纯API_vs_三层路由.png", "纯 API 与三层路由：上游 token 成本对比",
                          "同一模型、同一单价估算；展示调用成本，不代表产品售价",
                          ["纯 API", "三层路由\nAPI 兜底"],
                          [("估算费用（元/280条）", [row["estimated_cost_yuan"] for row in rows], "#1976A3")],
                          "元/280 条", decimals=3)
        return

    rows = report["cost"]
    values = [row["estimated_cost_yuan"] for row in rows]
    labels = ["纯 API", "三层路由\nAPI 兜底"]
    colors = ["#7B8794", "#1976A3"]
    fig, ax = plt.subplots(figsize=(10.5, 6.2), dpi=180)
    bars = ax.bar(labels, values, color=colors, width=0.52)
    ax.set_ylabel("估算上游费用（元/280 条）")
    ax.set_title("纯 API 与三层路由：上游 token 成本对比")
    ax.grid(axis="y", alpha=0.25)
    for bar, row in zip(bars, rows):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.025,
                f"¥{row['estimated_cost_yuan']:.6f}", ha="center", va="bottom", fontsize=10)
        ax.text(bar.get_x() + bar.get_width() / 2, -max(values) * 0.10,
                f"输入 {row['input_tokens']:,}\n输出 {row['output_tokens']:,}",
                ha="center", va="top", fontsize=8.5, color="#4B5563")
    saving = 1 - values[1] / values[0]
    ax.text(0.02, -0.25,
            f"同一模型和单价估算：输入 1 元/百万 token、输出 4 元/百万 token；"
            f"三层路由上游 token 用量约减少 {saving:.1%}。这不是产品售价。",
            transform=ax.transAxes, fontsize=8.5, color="#4B5563")
    fig.tight_layout(rect=(0, 0.17, 1, 1))
    fig.savefig(OUT / "费用对比_纯API_vs_三层路由.png", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    old = load_json(EVAL / "pure_api_vs_routed_report.json")
    latency_old = load_json(EVAL / "pure_api_vs_routed_latency.json")
    cost_old = old["large_batch_cost"]
    metrics = old["metrics"]
    latency = {
        "pure_api_seconds": latency_old["pure_api_real_replay"]["total_seconds"],
        "routed_rule_local_seconds": latency_old["routed"]["rule_and_local_seconds"],
        "routed_api_fallback_claims": latency_old["routed"]["route_counts"]["api_fallback"]
        if "api_fallback" in latency_old["routed"]["route_counts"]
        else old["route_counts"]["api"],
        "api_fallback_timing_status": latency_old["routed"].get("api_timing_status", "unknown"),
    }
    cost = [
        {
            "mode": "pure_api",
            "claims": 280,
            "api_calls": cost_old[0]["api_batches"],
            "input_tokens": cost_old[0]["input_tokens"],
            "output_tokens": cost_old[0]["output_tokens"],
            "estimated_cost_yuan": cost_old[0]["cost_yuan_per_report"],
            "measurement": "token estimate",
        },
        {
            "mode": "three_route_api_fallback",
            "claims": 280,
            "api_calls": cost_old[1]["api_batches"],
            "input_tokens": cost_old[1]["input_tokens"],
            "output_tokens": cost_old[1]["output_tokens"],
            "estimated_cost_yuan": cost_old[1]["cost_yuan_per_report"],
            "measurement": "token estimate",
        },
    ]
    report = {
        "title": "纯 API 与三层路由对比（72 条同集回放）",
        "dataset": old["dataset"],
        "dataset_claims": 72,
        "label_basis": "programmatic semantic rewrite; evaluation-only",
        "route_counts": old["route_counts"],
        "metrics": metrics,
        "latency": latency,
        "cost": cost,
        "current_v3_human_gold": {
            "claims_scored": 37,
            "abstained_claims": 3,
            "pure_api_status": "not_measured_same_set",
            "note": "当前人工 Gold 不能直接填入纯 API 柱；需在同一 37 条上实测后再更新主结论。",
        },
        "caveats": [
            "效果数据来自已完成的 72 条程序化语义改写 API 回放，不是人工 Gold。",
            "时间图的三层路由只实测规则+本地模型，API 兜底网络延迟未测。",
            "费用按同一 DeepSeek 单价（输入 1 元/M、输出 4 元/M）和 40 条/批估算，不代表产品售价。",
            "当前 v3 人工 Gold 的纯 API 尚未在同一环境完成，不能与本回放结果混合。",
        ],
    }
    (OUT / "报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    effect_rows = []
    for item in metrics:
        effect_rows.append({
            "mode": item["system"],
            "claims": item["claims"],
            "linked": item["linked"],
            "correct_association_rate": item["top1_accuracy"],
            "coverage": item["coverage"],
            "abstain_rate": item["abstain_rate"],
            "error_association_rate": item["error_association_rate"],
            "label_basis": item["label_basis"],
        })
    write_csv(OUT / "效果对比.csv", effect_rows)
    write_csv(OUT / "时间对比.csv", [
        {"mode": "pure_api", "seconds": latency["pure_api_seconds"], "measurement": "real API replay"},
        {"mode": "three_route_rule_local", "seconds": latency["routed_rule_local_seconds"], "measurement": "local measured; API fallback excluded"},
    ])
    write_csv(OUT / "费用对比.csv", cost)
    chart_effect(report)
    chart_latency(report)
    chart_cost(report)
    report_md = """# 纯 API 与三层路由对比

## 数据范围

本报告使用同一批 72 条语义改写任务，避免把不同数据集的结果拼在一起。标签为程序化语义改写评测标签，不能替代人工行业真值。

## 效果

| 模式 | 正确关联率 | 覆盖率 | 拒答率 | 错误关联率 |
|---|---:|---:|---:|---:|
"""
    report_md += "".join(
        f"| {row['mode']} | {row['correct_association_rate']:.2%} | {row['coverage']:.2%} | "
        f"{row['abstain_rate']:.2%} | {row['error_association_rate']:.2%} |\n"
        for row in effect_rows
    )
    report_md += f"""
三层路由的 72 条任务分流为：规则唯一 {old['route_counts']['rule_unique']} 条、本地模型 {old['route_counts']['local']} 条、API 兜底 {old['route_counts']['api']} 条；另有 {old['route_counts']['abstain']} 条拒答。

## 时间

- 纯 API：72 条真实网络回放，共 **{latency['pure_api_seconds']:.3f} 秒**。
- 三层路由规则+本地模型前置：**{latency['routed_rule_local_seconds']:.3f} 秒**。
- 三层路由的 {latency['routed_api_fallback_claims']} 条 API 兜底未在本机联网实测，因此不能把 9.087 秒写成端到端总耗时。

## 上游 token 成本

按同一模型、输入 1 元/百万 token、输出 4 元/百万 token、40 条/批估算：

| 模式 | 输入 token | 输出 token | 估算费用（元/280 条） |
|---|---:|---:|---:|
"""
    report_md += "".join(
        f"| {row['mode']} | {row['input_tokens']:,} | {row['output_tokens']:,} | {row['estimated_cost_yuan']:.6f} |\n"
        for row in cost
    )
    saving = 1 - cost[1]["estimated_cost_yuan"] / cost[0]["estimated_cost_yuan"]
    report_md += f"\n按该口径，三层路由的上游 token 用量约减少 **{saving:.1%}**；这只是调用量估算，不是产品定价。\n\n"
    report_md += "## 当前人工 Gold 状态\n\n当前 v3 人工 Gold 有 37 条可评分题、3 条人工拒答，但纯 API 尚未在同一批次完成，因此不将其填入上述主图。完成同集 API 实测后再更新主结论。\n"
    (OUT / "对比报告.md").write_text(report_md, encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), "report": str(OUT / "对比报告.md")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
