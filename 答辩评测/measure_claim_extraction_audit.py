"""Measure PDF claim extraction with an explicit false-positive audit queue.

The extractor has no gold labels in a raw annual report. This script therefore
reports three separate quantities: recognized candidates, deterministic safety
flags, and a review queue. It never calls a safety flag a human false positive.
After a reviewer labels the queue, ``confirmed_false`` becomes the number that
may be quoted as an error rate.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zhilian.engine import extract_claims  # noqa: E402
from zhilian.pdf_ingest import extract_pdf  # noqa: E402

METRIC_WORDS = re.compile(
    r"收入|营收|销售|利润|资产|负债|现金|成本|费用|产量|销量|占比|比例|金额|预算|支出|股本|人数|流量|回款|订单|产能|投资|研发|毛利|净利"
)


def _sentence_rows(pdf_path: Path, facts: list[dict]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text", sort=True) or ""
            for sentence_index, match in enumerate(re.finditer(r"[^。！？；;\n]+", text)):
                sentence = match.group().strip()
                if not sentence or not re.search(r"\d", sentence):
                    continue
                block = {"file_id": pdf_path.name, "location": ["pdf", page_number, sentence_index],
                         "label": f"PDF 第 {page_number} 页", "text": sentence}
                claims = extract_claims(block, facts)
                for claim_index, claim in enumerate(claims):
                    metric_like = bool(METRIC_WORDS.search(claim["original"]))
                    issue = claim.get("issue") or ""
                    # These are deterministic safety flags, not gold labels.
                    flags = []
                    if not metric_like:
                        flags.append("缺少指标语义词")
                    if issue:
                        flags.append("需要来源确认")
                    if claim["kind"] == "growth" and not claim.get("spec", {}).get("comparison_period_explicit", True):
                        flags.append("增长比较期间未明确")
                    rows.append({
                        "file": pdf_path.name, "page": page_number,
                        "sentence_index": sentence_index, "claim_index": claim_index,
                        "text": claim["original"], "kind": claim["kind"],
                        "refs": json.dumps(claim.get("refs", []), ensure_ascii=False),
                        "issue": issue, "metric_like": metric_like,
                        "safety_flags": "；".join(flags),
                        "review_label": "待人工标注", "confirmed_false": "",
                    })
    stats = {
        "numeric_sentences": None,
        "recognized_sentences": len({(r["page"], r["sentence_index"]) for r in rows}),
        "claims": len(rows),
        "flagged_claims": sum(bool(r["safety_flags"]) for r in rows),
        "review_queue": len(rows),
    }
    return rows, stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    extraction = extract_pdf(args.pdf, output_dir=args.out / "pdf_evidence")
    rows, stats = _sentence_rows(args.pdf, extraction.get("facts", []))
    with pymupdf.open(args.pdf) as document:
        numeric_sentences = 0
        for page in document:
            text = page.get_text("text", sort=True) or ""
            # Split *on* sentence terminators.  The previous expression split
            # on every non-terminator character, making the denominator almost
            # equal to the number of individual characters and understating
            # coverage by several orders of magnitude.
            numeric_sentences += sum(
                bool(re.search(r"\d", part.strip()))
                for part in re.split(r"[。！？；;\n]+", text)
                if part.strip()
            )
    stats["numeric_sentences"] = numeric_sentences
    stats["coverage"] = round(stats["recognized_sentences"] / max(numeric_sentences, 1), 6)
    stats["heuristic_suspected_false_rate"] = round(stats["flagged_claims"] / max(stats["claims"], 1), 6)
    stats["confirmed_false"] = None
    stats["confirmed_false_rate"] = None
    report = {
        "pdf": str(args.pdf.resolve()),
        "stats": stats,
        "label_basis": "规则安全旗标与人工复核队列；尚未形成该 PDF 的人工金标准，因此 flagged_claims 不能直接称为误抽取数",
        "next_step": "在 claim_audit_queue.csv 中填写 review_label=正确/误抽取/不确定，并将 confirmed_false 填为 1/0 后重新汇总",
    }
    (args.out / "claim_extraction_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.out / "claim_audit_queue.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        fields = list(rows[0]) if rows else ["file", "page", "text", "review_label", "confirmed_false"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
