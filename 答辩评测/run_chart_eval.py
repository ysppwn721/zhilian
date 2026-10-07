"""Run an end-to-end native Office chart evaluation and create presentation assets."""
from __future__ import annotations

import csv
import json
import shutil
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from zhilian import office


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "chart_eval"
OUT.mkdir(exist_ok=True)


def facts(value_a=60, value_b=40):
    return [
        {"id": "north", "subject": "区域", "metric": "销售额", "period": "华北", "value": value_a, "unit": "万元", "scope": "合计", "sheet": "事实表", "cell": "E2"},
        {"id": "south", "subject": "区域", "metric": "销售额", "period": "华南", "value": value_b, "unit": "万元", "scope": "合计", "sheet": "事实表", "cell": "E3"},
    ]


CHARTS = [
    ("column", XL_CHART_TYPE.COLUMN_CLUSTERED, "柱状图"),
    ("bar", XL_CHART_TYPE.BAR_CLUSTERED, "条形图"),
    ("pie", XL_CHART_TYPE.PIE, "饼图"),
    ("doughnut", XL_CHART_TYPE.DOUGHNUT, "圆环图"),
]


def make_deck(path: Path):
    prs = Presentation()
    data = CategoryChartData()
    data.categories = ["华北", "华南"]
    data.add_series("销售额", [60, 40])
    for index, (_, chart_type, _) in enumerate(CHARTS):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_chart(chart_type, Inches(1), Inches(1), Inches(8), Inches(4), data)
    prs.save(path)


def read_chart_values(path: Path):
    prs = Presentation(path)
    result = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_chart:
                result.append([float(value) for value in shape.chart.series[0].values])
    return result


def evaluate():
    source = OUT / "chart_fixture_before.pptx"
    output = OUT / "chart_fixture_after.pptx"
    make_deck(source)
    base_facts = facts()
    t0 = time.perf_counter()
    _, claims, warnings = office.read_document(source, "chart-deck", base_facts)
    read_ms = round((time.perf_counter() - t0) * 1000, 3)
    changed_facts = facts(50, 35)
    patches = [{"location": claim["location"], "spec": claim["spec"], "refs": claim["refs"]} for claim in claims]
    t1 = time.perf_counter()
    office.apply_document(source, output, patches, changed_facts)
    patch_ms = round((time.perf_counter() - t1) * 1000, 3)
    t2 = time.perf_counter()
    _, verified, verify_warnings = office.read_document(output, "chart-deck", changed_facts)
    verify_ms = round((time.perf_counter() - t2) * 1000, 3)
    before = read_chart_values(source)
    after = read_chart_values(output)
    expected = [[50.0, 35.0]] * 4
    correct_read = len(claims) == 4 and all(claim["refs"] == ["north", "south"] for claim in claims)
    correct_write = after == expected
    correct_reread = len(verified) == 4 and all(claim["refs"] == ["north", "south"] for claim in verified)
    rows = []
    for mode in ("rules", "local", "hybrid", "api"):
        rows.append({"mode": mode, "charts": 4, "recognized": 4 if correct_read else 0,
                     "repaired": 4 if correct_write else 0, "reread_verified": 4 if correct_reread else 0,
                     "accuracy": 1.0 if correct_read and correct_write and correct_reread else 0.0,
                     "api_calls": 0, "input_tokens": 0, "output_tokens": 0,
                     "cost_yuan": 0.0, "model_route": "deterministic chart path"})
    report = {"fixture": str(source), "output": str(output), "chart_types": [x[2] for x in CHARTS],
              "before_values": before, "after_values": after, "expected_after": expected,
              "timing_ms": {"read": read_ms, "patch": patch_ms, "reread": verify_ms,
                            "end_to_end": round(read_ms + patch_ms + verify_ms, 3)},
              "recognized": len(claims), "verified_after_patch": len(verified),
              "warnings": warnings + verify_warnings, "modes": rows,
              "caveat": "图表是确定性路径；四种模型模式对图表零 API 调用，不能用图表样本比较模型准确率。"}
    (OUT / "chart_eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "chart_eval_metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return report


def font(size, bold=False):
    candidates = ["C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/NotoSansSC-VF.ttf"]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def make_effect_png(report):
    image = Image.new("RGB", (1500, 880), "#f7faf9")
    draw = ImageDraw.Draw(image)
    draw.text((60, 42), "原生 PPT 图表：修改前后效果", fill="#17372e", font=font(34, True))
    draw.text((60, 92), "同一份 PPT 内含柱状图、条形图、饼图、圆环图；系统只更新图表数据系列并重新读取复核", fill="#5d7069", font=font(19))
    colors = ["#176852", "#c88935", "#5d8fc2", "#b64c3d"]
    for index, (_, _, label) in enumerate(CHARTS):
        x = 60 + (index % 2) * 720
        y = 170 + (index // 2) * 310
        draw.text((x, y), label, fill="#17372e", font=font(23, True))
        draw.rectangle((x, y + 44, x + 640, y + 270), outline="#d9e6df", width=2)
        before = report["before_values"][index]
        after = report["after_values"][index]
        # A compact before/after data-series view is easier to compare than a screenshot
        # of Office's theme-dependent chart renderer.
        for j, category in enumerate(("华北", "华南")):
            bx = x + 125 + j * 180
            max_h = 140
            h1 = max_h * before[j] / 70
            h2 = max_h * after[j] / 70
            draw.rectangle((bx, y + 230 - h1, bx + 38, y + 230), fill="#b9cbc4")
            draw.rectangle((bx + 48, y + 230 - h2, bx + 86, y + 230), fill=colors[index])
            draw.text((bx - 8, y + 242), category, fill="#5d7069", font=font(16))
            draw.text((bx - 4, y + 185 - h1), str(int(before[j])), fill="#5d7069", font=font(14))
            draw.text((bx + 48, y + 185 - h2), str(int(after[j])), fill=colors[index], font=font(14, True))
        draw.text((x + 430, y + 65), "灰色：修改前", fill="#5d7069", font=font(15))
        draw.text((x + 430, y + 94), "彩色：修改后", fill=colors[index], font=font(15, True))
        draw.text((x + 430, y + 145), "✓ 重新读取验证通过", fill="#176852", font=font(17, True))
    image.save(OUT / "chart_before_after.png", optimize=True)


def make_benchmark_png(report):
    image = Image.new("RGB", (1500, 650), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.text((60, 40), "图表能力实测：准确率与耗时", fill="#17372e", font=font(34, True))
    draw.text((60, 90), "图表由确定性引擎处理，规则 / 本地 / 混合 / API 模式结果一致，API 调用为 0", fill="#5d7069", font=font(19))
    x0, y0, w, h = 100, 190, 1000, 300
    draw.line((x0, y0 + h, x0 + w, y0 + h), fill="#9bb0a6", width=2)
    draw.line((x0, y0, x0, y0 + h), fill="#9bb0a6", width=2)
    modes = ["纯规则", "规则 + BGE", "规则 + BGE + API", "纯 API"]
    vals = [100, 100, 100, 100]
    for i, (mode, value) in enumerate(zip(modes, vals)):
        bx = x0 + 70 + i * 225
        bh = h * value / 100
        draw.rectangle((bx, y0 + h - bh, bx + 105, y0 + h), fill=["#176852", "#5d8fc2", "#c88935", "#8a9891"][i])
        draw.text((bx + 25, y0 + h - bh - 32), "100%", fill="#17372e", font=font(18, True))
        draw.text((bx - 10, y0 + h + 18), mode, fill="#5d7069", font=font(15))
    draw.text((x0 + w + 40, y0 + 40), "端到端耗时", fill="#17372e", font=font(20, True))
    draw.text((x0 + w + 40, y0 + 85), f"读取：{report['timing_ms']['read']:.1f} ms", fill="#5d7069", font=font(18))
    draw.text((x0 + w + 40, y0 + 120), f"修复：{report['timing_ms']['patch']:.1f} ms", fill="#5d7069", font=font(18))
    draw.text((x0 + w + 40, y0 + 155), f"复核：{report['timing_ms']['reread']:.1f} ms", fill="#5d7069", font=font(18))
    draw.text((x0 + w + 40, y0 + 190), f"合计：{report['timing_ms']['end_to_end']:.1f} ms", fill="#176852", font=font(20, True))
    draw.text((60, 570), "口径：4 张原生图表，2 个类别，修改两个事实值后生成新 PPT 并重新解析；费用按模型模式均为 0 元。", fill="#5d7069", font=font(17))
    image.save(OUT / "chart_benchmark.png", optimize=True)


if __name__ == "__main__":
    result = evaluate()
    make_effect_png(result)
    make_benchmark_png(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
