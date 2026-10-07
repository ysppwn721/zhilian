"""Render separate charts for the current human-Gold local-model experiment."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "答辩评测" / "v3_eval_20261005" / "v3_human_gold_40"


def font(size):
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def chart(path, title, subtitle, labels, values, ylabel, colors, labels_on_bars, note, max_value=None):
    from PIL import Image, ImageDraw
    width, height = 1900, 1100
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, small_font = font(38), font(21), font(20)
    draw.text((70, 35), title, fill="#172033", font=title_font)
    draw.text((72, 92), subtitle, fill="#536174", font=small_font)
    left, top, plot_w, plot_h = 160, 165, 1570, 650
    numeric = [v for v in values if v is not None]
    ceiling = max_value or (max(numeric) * 1.18 if numeric else 1.0)
    if ceiling <= 0:
        ceiling = 1.0
    for tick in range(6):
        value = ceiling * tick / 5
        y = top + plot_h - plot_h * value / ceiling
        draw.line((left, y, left + plot_w, y), fill="#DCE2E8", width=2)
        draw.text((left - 100, y - 11), f"{value:.0%}" if ceiling <= 1 else f"{value:.1f}", fill="#65717C", font=small_font)
    group_w = plot_w / len(labels)
    bar_w = max(24, int(group_w * 0.52))
    for index, (label, value, text, color) in enumerate(zip(labels, values, labels_on_bars, colors)):
        x = left + index * group_w + (group_w - bar_w) / 2
        if value is None:
            draw.rectangle((x, top + plot_h - 4, x + bar_w, top + plot_h), fill="#CBD5E1")
            draw.text((x + bar_w / 2, top + plot_h / 2), "未测", fill="#64748B", font=label_font, anchor="mm")
        else:
            y = top + plot_h - plot_h * value / ceiling
            draw.rectangle((x, y, x + bar_w, top + plot_h), fill=color)
            draw.text((x + bar_w / 2, max(top + 8, y - 32),), text, fill="#26323C", font=small_font, anchor="mm")
        draw.text((x + bar_w / 2, top + plot_h + 35), label, fill="#26323C", font=label_font, anchor="ma")
    draw.text((70, 930), ylabel, fill="#536174", font=small_font)
    draw.multiline_text((70, 970), note, fill="#536174", font=small_font, spacing=5)
    image.save(path, optimize=True)


def main():
    result = json.loads((OUT / "当前模型与自动路由比较.json").read_text(encoding="utf-8"))
    models = result["models"]
    names = ["通用 BGE", "BGE + 证据重排", "自动年报 BERT", "离线三层路由"]
    source = [models["bge"], models["bge_evidence_experiment"], models["annual_bert_auto_profile"], models["three_route_offline"]]
    quote = [item["quote_top1"] for item in source]
    growth = [item["growth_exact"] for item in source]
    chart(OUT / "效果对比_自动年报BERT.png", "当前人工 Gold：模型与自动年报路由效果",
          "37 条可评分题；3 条人工拒答不计入准确率；增长题为本期+上期来源集合精确匹配",
          names, quote, "单值 Top-1", ["#7B8794", "#2A7F9E", "#1976A3", "#155E75"],
          [f"{v:.1%}" for v in quote],
          "注：自动年报 BERT 由文档画像选择；离线三层路由规则唯一时直连，否则转年报 BERT。纯 API 未在这 37 条上同集实测。",
          max_value=1.0)
    chart(OUT / "效果对比_增长集合_自动年报BERT.png", "当前人工 Gold：增长来源集合效果",
          "要求同时选出本期与上期来源；不把单值题与增长题混为一个准确率",
          names, growth, "增长集合精确匹配", ["#7B8794", "#2A7F9E", "#1976A3", "#155E75"],
          [f"{v:.1%}" for v in growth],
          "证据重排 BGE 的权重只在程序化 v3 开发集选择，再在人工 Gold 上验证；尚未接入生产。",
          max_value=1.0)
    elapsed = [models["bge"]["elapsed_seconds"], None, models["annual_bert_auto_profile"]["elapsed_seconds"], models["three_route_offline"]["elapsed_seconds"]]
    chart(OUT / "时间对比_自动年报BERT.png", "当前人工 Gold：本地推理耗时",
          "同一 37 条人工 Gold；CPU 实测，模型加载和推理均包含在记录时间内",
          names, elapsed, "耗时（秒）", ["#7B8794", "#2A7F9E", "#1976A3", "#155E75"],
          [f"{v:.3f}s" if v is not None else "未单独测量" for v in elapsed],
          "三层路由耗时为规则+年报模型离线实验；API 兜底未调用，因此不能称为完整线上端到端耗时。",
          max_value=max(v for v in elapsed if v is not None) * 1.18)
    # Cost is intentionally a status chart: no remote call was made on this
    # current set, so a numeric API cost would be fabricated.
    chart(OUT / "费用对比_自动年报BERT.png", "当前人工 Gold：外部 API 调用成本状态",
          "本次实验未发送 API 请求；本地规则、BGE 和年报 BERT 不产生上游 token 费用",
          ["纯 API（当前 v3）", "离线三层路由", "年报 BERT", "BGE"], [None, 0.0, 0.0, 0.0],
          "费用（本次实验）", ["#CBD5E1", "#155E75", "#1976A3", "#7B8794"],
          ["未测", "¥0（未调用 API）", "¥0（本地）", "¥0（本地）"],
          "价格结论需在同一批次完成纯 API 和真实 API 兜底后再填入；旧 72 条回放费用不混入本图。",
          max_value=1.0)
    rows = []
    for name, item in zip(names, source):
        rows.append({"strategy": name, "quote_top1": item["quote_top1"], "growth_exact": item["growth_exact"],
                     "elapsed_seconds": item.get("elapsed_seconds"), "api_status": "not_measured"})
    with (OUT / "当前模型与自动路由图表数据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    md = """# 当前人工 Gold 模型与自动年报路由比较

本次图表使用同一批 37 条可评分人工 Gold，3 条人工拒答不计入模型准确率。

| 策略 | 单值 Top-1 | 增长集合精确匹配 | 耗时 |
|---|---:|---:|---:|
"""
    md += "".join(f"| {r['strategy']} | {r['quote_top1']:.2%} | {r['growth_exact']:.2%} | {r['elapsed_seconds'] if r['elapsed_seconds'] is not None else '—'} 秒 |\n" for r in rows)
    md += "\n自动年报识别已选择年报修正版 BERT；当前纯 API 没有在同一批次调用，价格图只显示本次实验的调用状态，不填未测数字。\n"
    (OUT / "当前模型与自动年报路由图表说明.md").write_text(md, encoding="utf-8")
    print(json.dumps({"output": str(OUT), "charts": ["效果对比_自动年报BERT.png", "效果对比_增长集合_自动年报BERT.png", "时间对比_自动年报BERT.png", "费用对比_自动年报BERT.png"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
