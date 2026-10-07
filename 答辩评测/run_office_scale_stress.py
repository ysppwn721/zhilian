"""Run the large Office-file smoke test used for the delivery report.

The test exercises the real HTTP import/append path with one Excel source,
one PPTX, and a configurable number of Word documents. It is deliberately a
standalone script rather than a default pytest case: the generated files are
synthetic load fixtures and the run takes longer than a normal regression test.
"""
from __future__ import annotations

import json
import argparse
import sys
import tempfile
import time
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.util import Inches

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhilian.app import create_app
from zhilian.demo import create_demo


def make_word(path: Path, index: int) -> Path:
    doc = Document()
    doc.add_heading(f"批量报告 {index:03d}", 0)
    doc.add_paragraph("本期销售额为125万元。")
    doc.add_paragraph(f"这是第 {index:03d} 份独立成果文档，用于批量导入压力验证。")
    doc.save(path)
    return path


def make_ppt(path: Path, pages: int = 24) -> Path:
    prs = Presentation()
    for index in range(1, pages + 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.7), Inches(0.7), Inches(11.5), Inches(4.8))
        frame = box.text_frame
        frame.text = f"第 {index:02d} 页：销售分析"
        frame.add_paragraph().text = "本期销售额为125万元。"
        frame.add_paragraph().text = "页面文本、页码和批量扫描压力验证。"
    prs.save(path)
    return path


def append(client: TestClient, wid: str, revision: int, paths: list[Path]):
    response = client.post(
        f"/api/projects/{wid}/documents",
        data={"revision": revision},
        files=[("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in paths],
    )
    if response.status_code != 200:
        raise RuntimeError(f"append failed: {response.status_code} {response.text}")
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--word-count', type=int, default=100,
                        help='总 Word 文档数（含初始导入的一份）')
    parser.add_argument('--ppt-pages', type=int, default=24)
    parser.add_argument('--output', type=Path,
                        help='结果 JSON 输出路径；默认写入 office_scale_stress_results.json')
    args = parser.parse_args()
    if args.word_count < 1 or args.ppt_pages < 1:
        raise SystemExit('word-count 和 ppt-pages 必须为正整数')
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="zhilian-office-scale-") as raw:
        root = Path(raw)
        client = TestClient(create_app(root / "state"))
        demo = create_demo(root / "demo")
        excel = next(p for p in demo if p.suffix == ".xlsx")
        first_word = next(p for p in demo if p.suffix == ".docx")
        initial = client.post(
            "/api/projects",
            data={"name": f"{args.ppt_pages}页PPT与{args.word_count}份Word压力测试"},
            files=[
                ("files", (excel.name, excel.read_bytes(), "application/octet-stream")),
                ("files", (first_word.name, first_word.read_bytes(), "application/octet-stream")),
            ],
        )
        initial.raise_for_status()
        state = initial.json()

        ppt = make_ppt(root / f"{args.ppt_pages}页汇报.pptx", args.ppt_pages)
        words = [make_word(root / f"批量报告_{i:03d}.docx", i) for i in range(1, args.word_count)]
        # Keep the same three-transaction shape at every scale so the timing
        # comparison measures document volume rather than a different code path.
        first_size = max(0, min(49, len(words)))
        second_start = first_size
        second_end = min(second_start + 50, len(words))
        batches = [[ppt, *words[:first_size]], words[second_start:second_end], words[second_end:]]
        batches = [batch for batch in batches if batch]
        batch_times = []
        for batch in batches:
            print(f"append batch size={len(batch)}")
            t0 = time.perf_counter()
            state = append(client, state["id"], state["revision"], batch)
            batch_times.append(round(time.perf_counter() - t0, 3))

        final = client.get(f"/api/projects/{state['id']}")
        final.raise_for_status()
        state = final.json()
        documents = state["documents"]
        result_docs = [d for d in documents if d["kind"] != "xlsx"]
        ppt_doc = next(d for d in result_docs if d["name"] == ppt.name)
        ppt_claims = [c for c in state["claims"] if c["file_id"] == ppt_doc["id"]]
        word_docs = [d for d in result_docs if d["name"].startswith("批量报告_")]
        word_claims = [c for c in state["claims"] if c["file_id"] in {d["id"] for d in word_docs}]
        result = {
            "documents_total": len(documents),
            "excel_sources": sum(d["kind"] == "xlsx" for d in documents),
            "word_documents": len(word_docs),
            "ppt_pages": len(ppt_claims),
            "ppt_claims": len(ppt_claims),
            "word_claims": len(word_claims),
            "batches": len(state.get("batches", [])),
            "batch_seconds": batch_times,
            "total_seconds": round(time.perf_counter() - started, 3),
            "summary_claims": state["summary"]["claims"],
            "status": "passed",
        }
    output = args.output or Path(__file__).with_name("office_scale_stress_results.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
