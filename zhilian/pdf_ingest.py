"""Optional PDF ingestion for public annual reports.

The product's primary workflow remains Office based.  This module is an
isolated, opt-in reader for annual-report PDFs and is deliberately conservative:
it extracts a text layer and financial tables when their structure is strong,
and emits chart candidates for OCR/manual review instead of inventing values.

Dependencies are imported lazily because the small desktop runtime does not
need PDF support.  The evaluation environment installs PyMuPDF and pdfplumber.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


FINANCIAL_TERMS = (
    "主要会计数据", "主要财务指标", "营业收入", "营业成本", "净利润",
    "利润总额", "资产总计", "负债合计", "现金流量", "本期数", "上年同期数",
    "变动比例", "本期比上年同期", "归属于上市公司股东",
)
PERIOD_RE = re.compile(r"(?:本期|本报告期|期末|上年同期|上期|期初|20\d{2}\s*年|第[一二三四]季度|本年度)")
CHANGE_RE = re.compile(r"变动|增减|同比|增长率|变化率")
CHANGE_PCT_RE = re.compile(r"(?:变动|增减|同比|变化率|增长率)[^\n]{0,15}(?:%|百分比|比例)")
NUM_RE = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")
UNIT_RE = re.compile(r"(?:（|\()\s*(元|万元|亿元|元/股|%)\s*(?:）|\))")


def clean_cell(value: Any) -> str:
    """Collapse layout whitespace, including spaces inserted between Chinese characters."""
    text = str(value or "").replace("\u00a0", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    # PDF table extractors frequently put a space between every Chinese glyph.
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return text


def to_number(value: Any) -> float | None:
    text = clean_cell(value).replace(",", "").replace("%", "")
    if not text or text in {"-", "—", "–", "不适用", "不适用/不确定"}:
        return None
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _repair_split_numeric_cells(row: list[str]) -> list[str]:
    """Repair a common PDF table artifact such as ``['1,', '032,869.20']``.

    The empty placeholder is retained so the table's column positions remain
    stable; this matters when the header has a merged first column.
    """
    fixed = list(row)
    for index in range(1, len(fixed) - 1):
        left = clean_cell(fixed[index])
        right = clean_cell(fixed[index + 1])
        if left.endswith(",") and re.match(r"^\d", right):
            fixed[index + 1] = left + right
            fixed[index] = ""
    return fixed


def _header_columns(rows: list[list[str]]) -> tuple[int | None, int | None, int | None]:
    """Find current, prior and reported-change columns in one to three header rows."""
    width = max((len(row) for row in rows), default=0)
    columns: list[str] = []
    for index in range(width):
        columns.append("".join(clean_cell(row[index]) if index < len(row) else "" for row in rows))
    current = prior = change = None
    percent_change = None
    for index, text in enumerate(columns):
        if not text:
            continue
        if percent_change is None and CHANGE_PCT_RE.search(text):
            percent_change = index
        if change is None and CHANGE_RE.search(text):
            change = index
            continue
        # Some PDF text layers drop the “年” suffix from one of a pair such
        # as “2022年 / 2021”.  Accept bare four-digit years here; the first
        # year is current and the following year is prior.
        year_header = r"20\d{2}\s*年?"
        if current is None and re.search(r"本期|本报告期|期末|" + year_header + r"|本年度", text):
            current = index
        elif prior is None and re.search(r"上期|上年同期|上年|期初|去年|" + year_header, text):
            # A year-only header is resolved below if it is ambiguous.
            prior = index
    if percent_change is not None:
        change = percent_change
    if current is not None and prior is not None and current != prior:
        return current, prior, change

    # Fallback for merged or year-only headers: select the first two columns
    # which contain numeric cells after the header.  A change column is excluded.
    numeric: list[int] = []
    for index in range(1, width):
        if change is not None and index == change:
            continue
        if any(index < len(row) and to_number(row[index]) is not None for row in rows[1:]):
            numeric.append(index)
    if current is None and numeric:
        current = numeric[0]
    if prior is None and len(numeric) > 1:
        prior = numeric[1]
    return current, prior, change


def _table_score(rows: list[list[Any]]) -> tuple[float, dict[str, int]]:
    normalized = [[clean_cell(cell) for cell in row] for row in rows]
    cells = [cell for row in normalized for cell in row if cell]
    numeric = sum(to_number(cell) is not None for cell in cells)
    headers = sum(any(term in cell for term in FINANCIAL_TERMS) for row in normalized[:3] for cell in row)
    periods = sum(bool(PERIOD_RE.search(cell)) for row in normalized[:3] for cell in row)
    score = len(cells) * 0.2 + numeric * 0.8 + headers * 3 + periods * 2
    return score, {"rows": len(rows), "cols": max((len(row) for row in rows), default=0),
                   "nonempty": len(cells), "numeric": numeric, "headers": headers}


def _calculated_change(label: str, current_value: float, prior_value: float,
                       current_raw: Any = "", prior_raw: Any = "") -> float | None:
    """Calculate the table's reported change using the metric's semantics.

    For a ratio metric such as “研发投入占营业收入比例”, the report usually
    publishes percentage-point change (3.84% - 3.15% = 0.69%), not relative
    growth (21.90%).  Treating these as the same was the main repeatable source
    of false validation failures in the annual-report batch.
    """
    ratio_metric = any(token in clean_cell(label) for token in ("占", "比例", "率"))
    ratio_cells = "%" in clean_cell(current_raw) or "%" in clean_cell(prior_raw)
    if ratio_metric or ratio_cells:
        return current_value - prior_value
    if prior_value == 0:
        return None
    return (current_value - prior_value) / abs(prior_value) * 100


def _extract_tables(page: Any) -> list[list[list[str]]]:
    """Extract tables with a small settings ensemble and de-duplicate results."""
    candidates: list[list[list[str]]] = []
    settings = [
        {},
        {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
        {"vertical_strategy": "text", "horizontal_strategy": "text",
         "text_x_tolerance": 2, "text_y_tolerance": 3},
    ]
    for config in settings:
        try:
            tables = page.extract_tables(table_settings=config) if config else page.extract_tables()
        except Exception:
            continue
        for table in tables or []:
            rows = [[clean_cell(cell) for cell in row] for row in table if row]
            if rows and any(cell for row in rows for cell in row):
                candidates.append(rows)
    # Keep the highest-quality table for an identical first-column signature.
    best: dict[tuple[str, ...], tuple[float, list[list[str]], dict[str, int]]] = {}
    for rows in candidates:
        key = tuple(row[0] if row else "" for row in rows[:8])
        score, stats = _table_score(rows)
        if key not in best or score > best[key][0]:
            best[key] = (score, rows, stats)
    return [item[1] for item in sorted(best.values(), key=lambda item: item[0], reverse=True)]


def _quarantine_conflicting_facts(
    facts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep ambiguous same-page metric/period values out of the fact list."""
    groups: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        key = (int(fact["source_page"]), clean_cell(fact["metric"]), fact["period"])
        groups[key].append(fact)

    conflicts: list[dict[str, Any]] = []
    rejected_ids: set[str] = set()
    for (page, metric, period), candidates in groups.items():
        values = {(item.get("value"), clean_cell(item.get("unit"))) for item in candidates}
        if len(values) <= 1:
            continue
        conflicts.append({
            "source_page": page,
            "metric": metric,
            "period": period,
            "reason": "同页同指标同期间识别出互相冲突的数值或单位，已隔离待人工复核",
            "candidates": [
                {key: item.get(key) for key in (
                    "id", "value", "unit", "source_file", "source_table", "source_row"
                )}
                for item in candidates
            ],
        })
        rejected_ids.update(str(item.get("id", "")) for item in candidates)

    accepted = [fact for fact in facts if str(fact.get("id", "")) not in rejected_ids]
    return accepted, conflicts


def _facts_from_table(rows: list[list[str]], source_file: str, page_number: int, table_number: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if len(rows) < 3:
        return [], None
    candidates = []
    for header_count in (1, 2, 3):
        current, prior, change = _header_columns(rows[:header_count])
        if current is not None and prior is not None and current != prior:
            header_text = "".join(clean_cell(cell) for row in rows[:header_count] for cell in row)
            period_signals = len(PERIOD_RE.findall(header_text))
            # Prefer a multi-row header that explicitly names the change
            # column.  If no change header exists, prefer the shortest header so
            # the first data row is not swallowed as metadata.
            candidates.append(((change is not None, period_signals, -header_count),
                               (header_count, current, prior, change)))
    chosen = max(candidates, key=lambda item: item[0])[1] if candidates else None
    if chosen is None:
        return [], None
    header_count, current, prior, change = chosen
    # Some PDFs lose the header font mapping (the Chinese header becomes
    # unreadable), while the data cells still retain a literal '%' suffix.
    # Prefer the column whose data cells visibly contain percentages.  This also
    # separates "变动金额" from "变动比例" when both are present.
    width = max((len(row) for row in rows), default=0)
    percent_counts = {
        index: sum(1 for row in rows[header_count:] if index < len(row) and "%" in clean_cell(row[index]))
        for index in range(1, width)
    }
    percent_column, percent_count = max(percent_counts.items(), key=lambda item: item[1], default=(None, 0))
    # Only use a data-derived percentage column when it is to the right of the
    # prior-period column.  Percentage-valued metrics such as "占收入比例"
    # often sit between current and prior and must not become the change column.
    if percent_column is not None and percent_count and percent_column > prior:
        if change is None or not any("%" in clean_cell(row[change]) for row in rows[header_count:] if change < len(row)):
            change = percent_column
    facts: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(rows[header_count:], header_count + 1):
        row = _repair_split_numeric_cells(raw_row)
        if not row:
            continue
        label = clean_cell(row[0])
        if not label or len(label) > 80 or not any(term in label for term in FINANCIAL_TERMS):
            continue
        current_value = to_number(row[current]) if current < len(row) else None
        prior_value = to_number(row[prior]) if prior < len(row) else None
        reported = to_number(row[change]) if change is not None and change < len(row) else None
        if current_value is None or prior_value is None:
            continue
        unit_match = UNIT_RE.search(label)
        unit = unit_match.group(1) if unit_match else "表内单位"
        base = f"pdf-p{page_number}-t{table_number}-r{row_number}"
        calculated = _calculated_change(label, current_value, prior_value,
                                        row[current], row[prior])
        has_cross_check = reported is not None and calculated is not None
        within_tolerance = not has_cross_check or abs(calculated - reported) <= 0.5
        cross_check_status = (
            "passed" if has_cross_check and within_tolerance
            else "failed" if has_cross_check
            else "not_available"
        )
        checks.append({"metric": label, "calculated_change": calculated, "reported_change": reported,
                       "within_tolerance": within_tolerance, "has_cross_check": has_cross_check,
                       "cross_check_status": cross_check_status, "source_file": source_file,
                       "source_page": page_number, "table_number": table_number,
                       "row_number": row_number, "unit": unit})
        # A mismatch usually means a split column, percentage-point change, or
        # an ambiguous scope. Keep it in the validation report, but do not let
        # it enter the usable fact list without an explicit future adjudication.
        if not within_tolerance:
            continue
        facts.extend([
            {"id": base + "-current", "metric": label, "period": "本期", "value": current_value,
             "unit": unit, "scope": f"{source_file}#p{page_number}", "source_page": page_number,
             "source_file": source_file, "source_table": table_number, "source_row": row_number,
             "has_cross_check": has_cross_check, "cross_check_status": cross_check_status},
            {"id": base + "-prior", "metric": label, "period": "上期", "value": prior_value,
             "unit": unit, "scope": f"{source_file}#p{page_number}", "source_page": page_number,
             "source_file": source_file, "source_table": table_number, "source_row": row_number,
             "has_cross_check": has_cross_check, "cross_check_status": cross_check_status},
        ])
    valid = sum(item["within_tolerance"] for item in checks)
    if not checks:
        return [], None
    validation = {"rows": len(checks), "reported_rows": sum(item["reported_change"] is not None for item in checks),
                  "cross_check_rows": sum(item["has_cross_check"] for item in checks),
                  "cross_check_pass": sum(item["cross_check_status"] == "passed" for item in checks),
                  "within_tolerance": valid, "checks": checks,
                  "header_count": header_count, "current_column": current, "prior_column": prior,
                  "change_column": change}
    return facts, validation


def extract_pdf(path: str | Path, output_dir: str | Path | None = None, max_pages: int = 500) -> dict[str, Any]:
    """Extract text, candidate financial tables, and chart/page evidence.

    The result is JSON serializable.  Chart candidates intentionally carry no
    values unless a future OCR/chart parser supplies them.
    """
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        raise ValueError("仅支持 PDF 文件")
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("PDF 超过 100 MB 上限")
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        import pdfplumber  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PDF 可选依赖缺失，请安装 pymupdf 和 pdfplumber") from exc

    started = time.perf_counter()
    text_pages: list[dict[str, Any]] = []
    chart_candidates: list[dict[str, Any]] = []
    with fitz.open(path) as document:
        if len(document) > max_pages:
            raise ValueError(f"PDF 超过 {max_pages} 页上限")
        for number, page in enumerate(document, 1):
            text = page.get_text("text", sort=True) or ""
            drawings = len(page.get_drawings())
            images = len(page.get_images(full=True))
            text_pages.append({"page": number, "chars": len(text), "text": text,
                               "drawings": drawings, "images": images})
            # A chart in a PDF is not necessarily an image: many annual reports
            # draw it as vector paths.  These pages are candidates only.
            # Financial tables also contain percentages and many vector lines.
            # Use explicit chart vocabulary for high precision; a page without
            # such labels is not claimed to be a chart even when it is visually
            # complex.  This intentionally favors review recall over false
            # chart positives.
            keyword = bool(re.search(r"(?:图\s*\d|图表|饼图|柱状图|条形图|折线图|趋势图|结构图|分布图|如下图)", text))
            if (images or drawings >= 40) and keyword:
                chart_candidates.append({"page": number, "chars": len(text), "images": images,
                                         "drawings": drawings, "text_preview": text[:500],
                                         "status": "candidate_needs_ocr_or_review"})

    candidate_pages = [
        item["page"] for item in text_pages
        if any(term in item["text"] for term in FINANCIAL_TERMS)
    ]
    tables: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    with pdfplumber.open(path) as pdf:
        for page_number in candidate_pages:
            if page_number > len(pdf.pages):
                continue
            page = pdf.pages[page_number - 1]
            for table_number, rows in enumerate(_extract_tables(page), 1):
                extracted, validation = _facts_from_table(rows, path.name, page_number, table_number)
                if validation is None:
                    continue
                stats = _table_score(rows)[1]
                table_record = {"page": page_number, "table": table_number, "rows": rows,
                                "stats": stats, "facts": len(extracted), "validation": validation}
                tables.append(table_record)
                facts.extend(extracted)

    # Duplicate rows are common because the same table appears in a summary and
    # detailed section. Keep the first occurrence but retain source provenance.
    unique: dict[tuple[str, str, float, str], dict[str, Any]] = {}
    for fact in facts:
        key = (fact["metric"], fact["period"], fact["value"], fact["scope"])
        unique.setdefault(key, fact)
    accepted_facts, fact_conflicts = _quarantine_conflicting_facts(list(unique.values()))
    chars = sum(item["chars"] for item in text_pages)
    result: dict[str, Any] = {
        "file": str(path), "pages": len(text_pages), "chars": chars,
        "text_layer_pages": sum(item["chars"] > 40 for item in text_pages),
        "text_layer_ratio": round(sum(item["chars"] > 40 for item in text_pages) / max(len(text_pages), 1), 4),
        "candidate_table_pages": len(candidate_pages), "tables": tables,
        "facts": accepted_facts, "fact_conflicts": fact_conflicts,
        "chart_candidates": chart_candidates,
        "ocr": {"available": False, "backend": None, "reason": "未安装 OCR 后端；扫描页和图表仅输出候选"},
        "timing_ms": {"end_to_end": round((time.perf_counter() - started) * 1000, 3)},
    }
    if output_dir:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        # Render only candidate chart pages.  This preserves evidence for review
        # without producing hundreds of unrelated page images.
        with fitz.open(path) as document:
            rendered_table_pages = sorted({table["page"] for table in tables})[:3]
            for page_number in rendered_table_pages:
                page = document[page_number - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                image_path = output / f"table_page_{page_number:04d}.png"
                pixmap.save(str(image_path))
            for candidate in chart_candidates:
                page = document[candidate["page"] - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                image_path = output / f"page_{candidate['page']:04d}.png"
                pixmap.save(str(image_path))
                candidate["rendered_image"] = str(image_path)
        (output / "extraction.json").write_text(
            __import__("json").dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result
