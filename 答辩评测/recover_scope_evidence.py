"""Recover nearby scope evidence for merged annual-report candidates.

This is evidence collection only.  It deliberately does not overwrite the
candidate's scope or assign labels: a page mentioning a subsidiary or a
consolidation change is not proof that the referenced table is consolidated.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import pymupdf


PATTERNS = {
    "consolidated": re.compile(r"合并(?:资产负债表|利润表|现金流量表|范围|口径|财务报表)"),
    "parent": re.compile(r"母公司(?:资产负债表|利润表|现金流量表|财务报表)|公司本部"),
    "segment": re.compile(r"分部|分产品|分行业|分地区|业务板块"),
}


def extract_context(text: str) -> list[dict]:
    hits = []
    for line_no, line in enumerate((text or "").splitlines(), 1):
        clean = re.sub(r"\s+", " ", line).strip()
        if not clean:
            continue
        kinds = [name for name, pattern in PATTERNS.items() if pattern.search(clean)]
        if kinds:
            hits.append({"line": line_no, "kinds": kinds, "text": clean[:300]})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        default=Path("答辩评测/annual_reports_merged/candidates_v2.jsonl"),
    )
    parser.add_argument(
        "--corpus", type=Path,
        default=Path("答辩评测/annual_reports_merged"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("答辩评测/annual_reports_merged/scope_evidence.jsonl"),
    )
    parser.add_argument(
        "--summary", type=Path,
        default=Path("答辩评测/annual_reports_merged/scope_evidence_summary.json"),
    )
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [row for row in rows if row.get("claim_kind") in {"growth", "quote"}]

    out_rows = []
    status_counts = Counter()
    for row in rows:
        path = args.corpus / str(row.get("source_file") or "")
        page_no = int(row.get("fact_page") or 0)
        pages = []
        all_hits = []
        if path.is_file():
            doc = pymupdf.open(path)
            for number in range(max(1, page_no - 1), min(len(doc), page_no + 1) + 1):
                text = doc[number - 1].get_text("text") or ""
                hits = extract_context(text)
                pages.append(number)
                all_hits.extend({"page": number, **hit} for hit in hits)
            doc.close()

        kinds = sorted({kind for hit in all_hits for kind in hit["kinds"]})
        existing = str(row.get("scope") or "").strip()
        if existing not in {"", "未标明", "unknown"}:
            status = "already_explicit_in_candidate"
        elif not all_hits:
            status = "no_nearby_scope_evidence"
        elif len(kinds) == 1:
            status = "single_scope_signal_needs_table_confirmation"
        else:
            status = "conflicting_scope_signals_needs_review"
        status_counts[status] += 1
        out_rows.append({
            "company": row.get("company"),
            "source_file": row.get("source_file"),
            "metric": row.get("metric"),
            "claim_kind": row.get("claim_kind"),
            "claim_page": row.get("claim_page"),
            "fact_page": page_no,
            "candidate_scope": row.get("scope"),
            "pages_checked": pages,
            "scope_kinds": kinds,
            "evidence": all_hits,
            "status": status,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in out_rows),
        encoding="utf-8",
    )
    summary = {
        "source": str(args.input),
        "rows": len(out_rows),
        "status_counts": dict(status_counts),
        "scope_kinds": dict(Counter(kind for row in out_rows for kind in row["scope_kinds"])),
        "training_ready": False,
        "note": "Evidence only; candidate scope and labels were not changed.",
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
