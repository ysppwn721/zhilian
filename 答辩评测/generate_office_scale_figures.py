"""Generate PNG evidence figures for the Office batch-stability slides.

The JSON inputs are produced by ``run_office_scale_stress.py`` through the
real FastAPI upload/append path. Pillow is used so the script runs in the
lightweight delivery environment without matplotlib.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "office_scale_figures"
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidate = Path(r"C:\Windows\Fonts\msyhbd.ttc") if bold else FONT_PATH
    if candidate.exists():
        return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def load(name: str) -> dict:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def text_center(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                fnt: ImageFont.FreeTypeFont, fill: str = "#253858", spacing: int = 4) -> None:
    left, top, right, bottom = box
    lines = text.split("\n")
    heights = [draw.textbbox((0, 0), line, font=fnt)[3] for line in lines]
    total = sum(heights) + spacing * max(0, len(lines) - 1)
    y = top + (bottom - top - total) / 2
    for line, height in zip(lines, heights):
        width = draw.textbbox((0, 0), line, font=fnt)[2]
        draw.text(((left + right - width) / 2, y), line, font=fnt, fill=fill)
        y += height + spacing


def canvas(width: int = 1500, height: int = 820) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), "white")
    return image, ImageDraw.Draw(image)


def footer(draw: ImageDraw.ImageDraw, text: str, y: int = 760) -> None:
    draw.text((65, y), text, font=font(22), fill="#667085")


def scale_time() -> None:
    data = [load("scale_10.json"), load("scale_50.json"), load("scale_100.json")]
    sizes = [10, 50, 100]
    times = [d["total_seconds"] for d in data]
    image, draw = canvas(height=880)
    draw.text((65, 38), "Office 批量规模压力测试：端到端耗时", font=font(38, True), fill="#253858")
    draw.text((65, 100), "真实 HTTP 上传、解析、论断抽取与状态提交（合成夹具）", font=font(24), fill="#667085")
    x0, y0, chart_w, chart_h = 160, 180, 1200, 480
    maxv = max(times) * 1.22
    draw.line((x0, y0, x0, y0 + chart_h), fill="#9aa5b1", width=3)
    draw.line((x0, y0 + chart_h, x0 + chart_w, y0 + chart_h), fill="#9aa5b1", width=3)
    for i in range(5):
        y = y0 + chart_h - chart_h * i / 4
        value = maxv * i / 4
        draw.line((x0, y, x0 + chart_w, y), fill="#e6e9ed", width=2)
        draw.text((62, y - 14), f"{value:.1f}", font=font(20), fill="#667085")
    colors = ["#4f86c6", "#3aa981", "#ef9b45"]
    bar_w, gap = 220, 170
    for idx, (size, value, item) in enumerate(zip(sizes, times, data)):
        x = x0 + 110 + idx * (bar_w + gap)
        h = chart_h * value / maxv
        draw.rounded_rectangle((x, y0 + chart_h - h, x + bar_w, y0 + chart_h), radius=12, fill=colors[idx])
        text_center(draw, (x - 30, y0 + chart_h + 20, x + bar_w + 30, y0 + chart_h + 70), str(size), font(28, True), "#253858")
        text_center(draw, (x - 45, y0 + chart_h - h - 78, x + bar_w + 45, y0 + chart_h - h - 10),
                    f"{value:.2f} s\n{item['documents_total']} 个文件", font(23, True), "#253858")
    draw.text((x0 + 410, 735), "Word 文档数（每组另含 1 份 Excel + 24 页 PPT）", font=font(23), fill="#253858")
    footer(draw, "数据来源：office_scale_stress（每个规模独立运行一次）", 815)
    image.save(OUT / "batch_scale_time.png")


def distribution() -> None:
    item = load("scale_100.json")
    labels = ["Excel 事实源", "Word 成果文档", "PPT 页级论断"]
    values = [item["excel_sources"], item["word_documents"], item["ppt_pages"]]
    colors = ["#2d6cdf", "#3aa981", "#ef9b45"]
    image, draw = canvas()
    draw.text((65, 38), "多文档项目组成（100 Word + 24 页 PPT 实测）", font=font(38, True), fill="#253858")
    draw.text((65, 100), "一份 Excel 事实源对应多份成果文件，按批次追加", font=font(24), fill="#667085")
    x0, y0, chart_w, row_h = 250, 220, 1050, 95
    for i, (label, value, color) in enumerate(zip(labels, values, colors)):
        y = y0 + i * 145
        draw.text((70, y + 23), label, font=font(27, True), fill="#253858")
        width = int(chart_w * value / 110)
        draw.rounded_rectangle((x0, y, x0 + width, y + row_h), radius=14, fill=color)
        draw.text((x0 + width + 20, y + 24), str(value), font=font(30, True), fill="#253858")
    draw.text((250, 690), f"总文件 {item['documents_total']} 个 · {item['batches']} 个批次 · {item['summary_claims']} 条汇总论断 · 状态：{item['status']}", font=font(24), fill="#253858")
    footer(draw, "数据来源：100 Word / 24 页 PPT 真实 HTTP 批量压力测试")
    image.save(OUT / "batch_distribution.png")


def box(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], label: str, fill: str) -> None:
    draw.rounded_rectangle(xy, radius=18, fill=fill, outline="#718096", width=3)
    text_center(draw, xy, label, font(25, True))


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int]) -> None:
    draw.line((*start, *end), fill="#718096", width=4)
    x, y = end
    draw.polygon([(x, y), (x - 18, y - 10), (x - 18, y + 10)], fill="#718096")


def failure_isolation() -> None:
    image, draw = canvas(1650, 860)
    draw.text((65, 38), "多文档批量容错：单个坏文件不拖垮已成功成果", font=font(38, True), fill="#253858")
    draw.text((65, 100), "continue_on_error=true：逐文件提交，失败项进入 rejected", font=font(24), fill="#667085")
    box(draw, (65, 320, 360, 500), "批量上传\n正常 + 损坏 + 重复", "#eaf2ff")
    box(draw, (500, 175, 790, 345), "正常文件\n逐个提交", "#e9f8f1")
    box(draw, (500, 475, 790, 645), "损坏 / 重复文件\n单独拒绝", "#fff2e6")
    box(draw, (930, 175, 1190, 345), "accepted\n成果保留", "#e9f8f1")
    box(draw, (930, 475, 1190, 645), "rejected\n文件名 + 原因", "#fff2e6")
    box(draw, (1320, 320, 1575, 500), "partial\n可重试", "#f0ecff")
    arrow(draw, (360, 390), (500, 260)); arrow(draw, (360, 430), (500, 560))
    arrow(draw, (790, 260), (930, 260)); arrow(draw, (790, 560), (930, 560))
    arrow(draw, (1190, 260), (1320, 390)); arrow(draw, (1190, 560), (1320, 430))
    footer(draw, "实测：正常 docx + 损坏 docx → 1 accepted、1 rejected、项目版本前进一次。", 775)
    image.save(OUT / "batch_failure_isolation.png")


def agent_flow() -> None:
    image, draw = canvas(1800, 710)
    draw.text((65, 38), "一份事实源对应多份成果文件：知链批量智能体流程", font=font(38, True), fill="#253858")
    labels = ["Excel\n事实源", "多份 Word / PPT\n批量追加", "规则解析\n确定性核验", "本地 BGE\n语义召回", "API 兜底\n困难样本", "人工确认\n修复与复核"]
    fills = ["#eaf2ff", "#eaf2ff", "#f0ecff", "#e9f8f1", "#fff2e6", "#f0ecff"]
    x, y, w, h, gap = 55, 265, 255, 135, 42
    for i, (label, fill) in enumerate(zip(labels, fills)):
        left = x + i * (w + gap)
        box(draw, (left, y, left + w, y + h), label, fill)
        if i < len(labels) - 1:
            arrow(draw, (left + w, y + h // 2), (left + w + gap - 8, y + h // 2))
    draw.text((65, 165), "先批量解析，再分层召回，最后由用户批准写回；模型不直接裁决和改文件。", font=font(28, True), fill="#253858")
    footer(draw, "规则负责计算与审计 · 本地模型负责语义候选 · API 只处理困难样本", 605)
    image.save(OUT / "office_agent_flow.png")


def chart_risk() -> None:
    image, draw = canvas(1800, 820)
    draw.text((65, 38), "原生图表覆盖：按结构风险分层", font=font(38, True), fill="#253858")
    draw.text((65, 105), "能读取 ≠ 能安全修改；只有结构可证明时才进入确定性写回", font=font(24), fill="#667085")
    columns = [
        (65, "自动核验", "单系列原生\n柱状 / 条形 / 折线\n饼图 / 圆环图", "#e9f8f1", "事实映射 → 修复 → 回读"),
        (650, "系列级确认", "单绘图区\n多系列原生图表", "#fff2e6", "每个系列独立候选与确认"),
        (1235, "人工复核提示", "图片图表\n组合图 / 多绘图区\n外部工作簿链接", "#f0ecff", "不自动猜测、不静默修改"),
    ]
    for x, title, body, fill, foot in columns:
        box(draw, (x, 225, x + 500, 585), body, fill)
        draw.text((x + 20, 245), title, font=font(30, True), fill="#253858")
        draw.text((x + 20, 510), foot, font=font(21), fill="#667085")
    footer(draw, "修复后重新读取 PPTX 图表缓存；视觉渲染需本机 Office/LibreOffice，未安装时明确标记未执行。", 755)
    image.save(OUT / "native_chart_risk.png")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    scale_time()
    distribution()
    failure_isolation()
    agent_flow()
    chart_risk()
    print(f"generated figures in {OUT}")


if __name__ == "__main__":
    main()
