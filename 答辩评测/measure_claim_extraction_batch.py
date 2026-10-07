"""Batch-measure正文论断抽取覆盖率 on a frozen set of annual-report PDFs.

This is a coverage/engineering benchmark, not a semantic gold set.  The
denominator is objective (sentences containing a digit in pages 10--45), while
the numerator is the current deterministic extractor's recognized sentences.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zhilian.engine import EXTRACTION_VERSION, extract_claims  # noqa: E402


def measure(path: Path, start_page: int, end_page: int) -> dict:
    started = time.perf_counter()
    doc = pymupdf.open(path)
    upper = min(end_page, len(doc))
    text = "\n".join(doc.load_page(i).get_text("text") or "" for i in range(start_page, upper))
    doc.close()
    sentences = [s.strip() for s in re.split(r"[。！？；;\n]", text) if s.strip()]
    numeric = [s for s in sentences if re.search(r"\d", s)]
    recognized = []
    kinds = Counter()
    for index, sentence in enumerate(numeric):
        claims = extract_claims({
            "file_id": path.name,
            "location": ["pdf", start_page + 1],
            "label": "batch-benchmark",
            "text": sentence,
        }, [])
        if claims:
            recognized.append(index)
            kinds.update(c["kind"] for c in claims)
    elapsed = time.perf_counter() - started
    return {
        "file": path.name,
        "pages_read": max(0, upper - start_page),
        "sentences": len(sentences),
        "numeric_sentences": len(numeric),
        "recognized_sentences": len(recognized),
        "recognition_rate": round(len(recognized) / len(numeric), 6) if numeric else None,
        "claims": sum(kinds.values()),
        "quote_claims": kinds.get("quote", 0),
        "growth_claims": kinds.get("growth", 0),
        "threshold_claims": kinds.get("threshold", 0),
        "ranking_claims": kinds.get("ranking", 0),
        "elapsed_seconds": round(elapsed, 3),
        "extraction_version": EXTRACTION_VERSION,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, default=Path(__file__).parent / "cn_reports")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).parent / "claim_extraction_batch_v1")
    parser.add_argument("--start-page", type=int, default=9,
                        help="zero-based inclusive page, default is page 10")
    parser.add_argument("--end-page", type=int, default=45,
                        help="zero-based exclusive page, default is page 45")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(args.reports.glob("*.pdf")):
        try:
            rows.append(measure(path, args.start_page, args.end_page))
        except Exception as exc:  # keep one damaged report from hiding the batch result
            rows.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})

    ok = [r for r in rows if "error" not in r]
    totals = {
        "reports": len(rows),
        "successful_reports": len(ok),
        "numeric_sentences": sum(r["numeric_sentences"] for r in ok),
        "recognized_sentences": sum(r["recognized_sentences"] for r in ok),
        "claims": sum(r["claims"] for r in ok),
        "recognition_rate": round(
            sum(r["recognized_sentences"] for r in ok) /
            sum(r["numeric_sentences"] for r in ok), 6
        ) if ok and sum(r["numeric_sentences"] for r in ok) else None,
        "elapsed_seconds": round(sum(r["elapsed_seconds"] for r in ok), 3),
        "page_range": {"start_zero_based": args.start_page, "end_exclusive": args.end_page},
        "denominator_definition": "sentences containing an ASCII digit in the selected pages",
        "label_basis": "deterministic extractor coverage benchmark; not semantic gold",
        "extraction_version": EXTRACTION_VERSION,
    }
    payload = {"totals": totals, "reports": rows}
    (args.out / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = sorted({key for row in rows for key in row})
    with (args.out / "reports.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(totals, ensure_ascii=False))


if __name__ == "__main__":
    main()
