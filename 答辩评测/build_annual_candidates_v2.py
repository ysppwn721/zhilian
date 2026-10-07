"""从独立年报生成待审计候选，不直接生成可训练真值。

输出保留正文页码、事实页码、表格行列、原始单元格和期间。所有记录默认
``ready_for_training=false``；主体/口径/单位以及标签仍需独立审计。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

FROZEN = {
    "000615", "000930", "002097", "002388", "002413", "002425", "002569",
    "002598", "002808", "002825", "300149", "300632", "600080", "600165",
    "600187", "603839", "688152", "836263", "873576",
}
METRICS = (
    "营业收入", "营业总收入", "主营业务收入", "营业成本", "净利润", "归属于上市公司股东的净利润",
    "利润总额", "资产总计", "负债合计", "应收账款", "存货", "销售费用", "管理费用",
    "研发费用", "货币资金", "现金及现金等价物", "经营活动产生的现金流量净额",
    "基本每股收益", "加权平均净资产收益率", "营业利润", "财务费用", "固定资产",
)
ALIASES = {
    "营业收入": ("营收", "销售收入", "营业总收入", "主营业务收入"),
    "净利润": ("纯利润",),
    "销售费用": ("销售支出",),
    "管理费用": ("管理支出",),
    "研发费用": ("研发支出",),
    "资产总计": ("总资产",),
    "负债合计": ("总负债",),
}
NUM = re.compile(r"[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?")
CURRENT = re.compile(r"本期|本报告期|期末|本年度|报告期")
PRIOR = re.compile(r"上期|上年同期|上年|期初|上年度|去年同期")
GROWTH = re.compile(r"同比|环比|增长|增加|减少|下降|上升|增幅|增减|变动")
YEAR_HEADER = re.compile(r"^20\d{2}\s*年(?:度)?$")


def clean(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def number(value: object) -> float | None:
    text = clean(value).replace(",", "").replace("−", "-")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    text = text.rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def norm_number(value: object) -> str:
    parsed = number(value)
    if parsed is None:
        return clean(value)
    return f"{parsed:.12g}"


def split_sentences(page_text: str) -> list[str]:
    """Split narrative text while retaining PDF line-wrapped anchors.

    PDF text extraction frequently puts a metric on one line and its value on
    the next line (especially in headline tables).  A newline-only split made
    those facts look unanchored.  Keep ordinary punctuation fragments and add
    short overlapping line windows as retrieval evidence.  These windows are
    candidates only; labels and accounting scope still require audit.
    """
    text = page_text or ""
    fragments = [
        re.sub(r"\s+", " ", item).strip()
        for item in re.split(r"[。！？；;\n]", text)
        if 8 <= len(item.strip()) <= 260 and any("\u4e00" <= c <= "\u9fff" for c in item)
    ]
    lines = [re.sub(r"\s+", " ", item).strip() for item in text.splitlines() if item.strip()]
    windows = []
    for start in range(len(lines)):
        window = " ".join(lines[start:start + 6]).strip()
        if 8 <= len(window) <= 320 and any("\u4e00" <= c <= "\u9fff" for c in window):
            windows.append(window)
    # Preserve order while removing duplicate fragments created by overlap.
    return list(dict.fromkeys(fragments + windows))


def metric_match(metric: str, sentence: str) -> bool:
    return any(token in sentence for token in (metric, *ALIASES.get(metric, ())))


def sentence_has_number(sentence: str, value: object) -> bool:
    target = norm_number(value)
    return any(norm_number(m.group(0)) == target for m in NUM.finditer(sentence))


def company_split(code: str) -> str:
    digest = int(hashlib.sha256(code.encode()).hexdigest()[:8], 16) % 100
    return "train" if digest < 70 else ("dev" if digest < 85 else "test")


def classify_columns(rows: list[list[object]]) -> tuple[int, int] | None:
    width = max((len(row) for row in rows), default=0)
    joined = []
    for index in range(width):
        joined.append("".join(clean(row[index]) if index < len(row) else "" for row in rows[:3]))
    current = next((i for i, value in enumerate(joined) if CURRENT.search(value) and not PRIOR.search(value)), None)
    prior = next((i for i, value in enumerate(joined) if PRIOR.search(value)), None)
    # Many annual reports use year headers instead of the words 本期/上期.
    # Treat the newest two explicit year columns as current/prior; this is a
    # retrieval heuristic only and remains subject to scope/period review.
    if current is None or prior is None:
        year_columns = []
        for index, value in enumerate(joined):
            match = YEAR_HEADER.fullmatch(value.strip())
            if match:
                year_columns.append((index, int(value[:4])))
        if len(year_columns) >= 2:
            ordered = sorted(year_columns, key=lambda item: item[1], reverse=True)
            current = ordered[0][0]
            prior = ordered[1][0]
    if current is None or prior is None or current == prior:
        return None
    return current, prior


def table_header(rows: list[list[object]]) -> tuple[int, int, int] | None:
    """Return (current column, prior column, first data row).

    Annual reports use one to three header rows.  The first row containing a
    metric label and numeric values is data; do not assume a fixed three-row
    header because that silently drops headline metrics.
    """
    for header_rows in (1, 2, 3):
        columns = classify_columns(rows[:header_rows])
        if not columns:
            continue
        current, prior = columns
        for data_row in range(header_rows, len(rows)):
            label = clean(rows[data_row][0]) if rows[data_row] else ""
            if label and any(number(cell) is not None for cell in rows[data_row][1:]):
                return current, prior, data_row
    return None


def extract_report(path: Path, code: str, max_table_pages: int) -> tuple[list[dict], list[dict], int]:
    import pdfplumber
    import pymupdf

    doc = pymupdf.open(path)
    sentences: list[dict] = []
    for page_no, page in enumerate(doc, 1):
        for sentence in split_sentences(page.get_text("text") or ""):
            sentences.append({"page": page_no, "text": sentence})
    doc.close()

    facts: list[dict] = []
    table_rows: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages[:max_table_pages], 1):
            for table_no, table in enumerate(page.extract_tables() or [], 1):
                header = table_header(table)
                if not header:
                    continue
                current_col, prior_col, first_data_row = header
                for row_no, row in enumerate(table[first_data_row:], first_data_row + 1):
                    label = clean(row[0]) if row else ""
                    exact_metric = next((candidate for candidate in METRICS if label == candidate), None)
                    metric = exact_metric or next((candidate for candidate in METRICS if candidate in label), None)
                    if not metric or len(label) > 60:
                        continue
                    # A composite ratio/segment row is not the same fact as the
                    # embedded metric (e.g. "研发投入总额占营业收入比例").
                    # Keep it out until a dedicated ratio schema is audited.
                    if exact_metric is None and any(token in label for token in ("占", "比例", "率", "同比", "变动")):
                        continue
                    cur = number(row[current_col]) if current_col < len(row) else None
                    prior = number(row[prior_col]) if prior_col < len(row) else None
                    if cur is None or prior is None:
                        continue
                    base = {
                        "company": code, "metric": metric, "rendered_metric": label,
                        "unit": "unknown", "scope": "unknown", "table_page": page_no,
                        "table_no": table_no, "row": row_no, "current_raw": clean(row[current_col]),
                        "prior_raw": clean(row[prior_col]),
                    }
                    current_id = f"{code}-p{page_no}-t{table_no}-r{row_no}-current"
                    prior_id = f"{code}-p{page_no}-t{table_no}-r{row_no}-prior"
                    facts.extend([
                        {"fact_id": current_id, "period": "current", "value": cur, **base},
                        {"fact_id": prior_id, "period": "prior", "value": prior, **base},
                    ])
                    table_rows.append({"metric": metric, "current": current_id, "prior": prior_id,
                                       "page": page_no, "table": table_no, "row": row_no})
    return facts, sentences, len(sentences)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("答辩评测/annual_reports_new_20261002"))
    parser.add_argument("--out", type=Path, default=Path("答辩评测/annual_report_candidates_v2_20261002"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-table-pages", type=int, default=80)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = rows[: args.limit]
    candidates: list[dict] = []
    stats = []
    for source in rows:
        code = source["stock_code"]
        if code in FROZEN:
            continue
        facts, sentences, sentence_count = extract_report(args.corpus / source["local_file"], code, args.max_table_pages)
        by_id = {fact["fact_id"]: fact for fact in facts}
        current_hits = prior_hits = growth_hits = 0
        for fact in facts:
            hits = [item for item in sentences if metric_match(fact["metric"], item["text"]) and sentence_has_number(item["text"], fact["value"])]
            if not hits:
                continue
            has_period = any(CURRENT.search(item["text"]) for item in hits) if fact["period"] == "current" else any(PRIOR.search(item["text"]) for item in hits)
            if fact["period"] == "current":
                current_hits += 1
            else:
                prior_hits += 1
            for anchor in hits[:2]:
                growth_sentence = bool(GROWTH.search(anchor["text"]))
                candidates.append({
                    "claim_id": f"{code}-p{anchor['page']}-{len(candidates)+1:05d}",
                    "claim_text": anchor["text"], "claim_page": anchor["page"],
                    "fact_id": fact["fact_id"], "fact": fact, "label": None,
                    "label_status": "pending_scope_period_audit",
                    "anchor_has_period_word": has_period,
                    "anchor_kind": "growth_sentence" if growth_sentence else f"{fact['period']}_anchor",
                    "split": company_split(code),
                    "source_pdf": source["local_file"], "source_sha256": source["sha256"],
                })
                if GROWTH.search(anchor["text"]):
                    growth_hits += 1
        stats.append({"company": code, "file": source["local_file"], "sentences": sentence_count,
                      "facts": len(facts), "current_fact_anchors": current_hits,
                      "prior_fact_anchors": prior_hits, "growth_anchor_sentences": growth_hits})
    args.out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "dev", "test"):
        with (args.out / f"{split}.jsonl").open("w", encoding="utf-8") as stream:
            for row in candidates:
                if row["split"] == split:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "ready_for_training": False,
        "validation_status": "pending_label_scope_period_and_candidate_audit",
        "source": str(args.corpus), "reports_processed": len(stats),
        "candidate_rows": len(candidates), "company_disjoint_split": True,
        "labels_are": "unassigned; anchors are retrieval evidence only",
        "stats": stats,
        "blocking_reasons": [
            "label_scope_and_period_audit_not_approved",
            "growth_expression_audit_pending",
            "candidate_set_balance_audit_pending",
        ],
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "README.md").write_text(
        "本目录是年报正文—表格候选的待审计产物，不是训练集。label=null 表示尚未确认主体、口径、期间、单位和正负标签。\n",
        encoding="utf-8",
    )
    print(json.dumps({"reports": len(stats), "candidate_rows": len(candidates), "stats": stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
