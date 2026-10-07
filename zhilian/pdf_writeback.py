"""Conservative preflight and verification for PDF numeric write-back.

PDF write-back is intentionally *not* implemented here.  A PDF is a printed
coordinate stream rather than a document model like DOCX.  This module makes
the safe decision before any future writer is allowed to run and provides the
post-write checks that a writer must satisfy.

The default outcome is ``errata_only``.  A caller must explicitly opt in to
write-back and provide user confirmation, and every hard safety check must
pass.  Unknown checks are never treated as passing.
"""
from __future__ import annotations

import hashlib
import io
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


_DECISION_ERRATA = "errata_only"
_DECISION_WRITE = "writeback_allowed"
_DECISION_REJECT = "reject"
_UNIT_FACTORS = {"元": Decimal("1"), "万元": Decimal("10000"), "万": Decimal("10000"),
                 "亿元": Decimal("100000000"), "亿": Decimal("100000000")}
_UNIT_LABELS = {"元": ("元",), "万元": ("万元", "万"), "亿元": ("亿元", "亿")}


def _load_fitz():
    """Load PyMuPDF lazily so the desktop Office runtime stays lightweight."""
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised by deployments
            raise RuntimeError("PDF 预检需要可选依赖 pymupdf") from exc
    return fitz


def _normalise_change(change: dict[str, Any]) -> dict[str, Any]:
    """Accept the names used by reports and by the Office repair workflow."""
    page = change.get("page", change.get("source_page"))
    before = change.get("old", change.get("old_value", change.get("before")))
    after = change.get("new", change.get("new_value", change.get("after")))
    if page is None or before is None or after is None:
        raise ValueError("PDF 写回项必须包含 page、old/before 和 new/after")
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效页码: {page!r}") from exc
    if page < 1:
        raise ValueError("PDF 页码从 1 开始")
    before = str(before)
    after = str(after)
    if not before or not after:
        raise ValueError("PDF 写回的旧值和新值不能为空")
    return {
        "page": page,
        "old": before,
        "new": after,
        # A source fact can have a different rendered form from the prose
        # value (for example 6.04 亿 vs 604,539,544.17).  Callers may provide
        # those equivalent renderings so the conservative duplicate check can
        # catch a table occurrence too.
        "equivalent_values": [
            str(value) for value in (
                change.get("equivalent_values", change.get("aliases", [])) or []
            ) if str(value)
        ],
        "source_value": change.get("source_value"),
        "source_unit": str(change.get("source_unit", change.get("unit", "")) or ""),
        "anchor": str(change.get("anchor", change.get("claim", "")) or ""),
        "kind": str(change.get("kind", "quote") or "quote"),
        "source": str(change.get("source", "") or ""),
    }


def _decimal_source_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        return Decimal(text) if text else None
    except (InvalidOperation, ValueError):
        return None


def _number_text(value: Decimal, decimals: int, *, grouping: bool = False,
                 rounding: str = "round") -> str:
    quantum = Decimal(1).scaleb(-decimals)
    if rounding == "truncate":
        quantized = value.quantize(quantum, rounding=ROUND_DOWN if value >= 0 else ROUND_DOWN)
    else:
        quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(quantized, f",.{decimals}f" if grouping else f".{decimals}f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _source_value_variants(value: Any, unit: str) -> list[str]:
    """Render a source fact in common PDF financial display forms.

    The output is intentionally finite and auditable: original unit plus the
    two neighbouring Chinese currency units, 0-3 decimal places, both
    half-up rounding and truncation, and grouped/ungrouped numeric forms.
    It is used only to find a duplicate source, so a false positive safely
    downgrades a candidate to review rather than changing a file.
    """
    source = _decimal_source_value(value)
    source_unit = str(unit or "").strip()
    if source is None or source_unit not in _UNIT_FACTORS:
        return []
    base_yuan = source * _UNIT_FACTORS[source_unit]
    variants: set[str] = set()
    for label, factor in (("元", _UNIT_FACTORS["元"]),
                          ("万元", _UNIT_FACTORS["万元"]),
                          ("亿元", _UNIT_FACTORS["亿元"])):
        scaled = base_yuan / factor
        labels = _UNIT_LABELS[label]
        for decimals in range(0, 4):
            for rounding in ("round", "truncate"):
                plain = _number_text(scaled, decimals, rounding=rounding)
                grouped = _number_text(scaled, decimals, grouping=True, rounding=rounding)
                for number in {plain, grouped}:
                    if not number:
                        continue
                    variants.add(number)
                    for display_label in labels:
                        variants.add(f"{number}{display_label}")
                        variants.add(f"{number} {display_label}")
    return sorted(variants, key=lambda item: (-len(item), item))


def _normalised_text(text: str) -> str:
    # Text extraction frequently inserts line breaks between a number and its
    # unit.  Removing whitespace is safe for an occurrence check because the
    # original text is also retained in the result for human review.
    return re.sub(r"\s+", "", text or "")


def _occurrences(text: str, value: str) -> int:
    """Count exact and whitespace-insensitive occurrences without double count."""
    if not value:
        return 0
    numeric_value = bool(re.fullmatch(r"-?[\d,]+(?:\.\d+)?%?", value.strip()))
    if numeric_value:
        # Do not treat the ``6`` in ``604539544.17`` as a separate rendering.
        exact = len(re.findall(r"(?<![\d.])" + re.escape(value) + r"(?![\d.])", text))
    else:
        exact = text.count(value)
    normal_text = _normalised_text(text)
    normal_value = _normalised_text(value)
    if numeric_value:
        normal = len(re.findall(r"(?<![\d.])" + re.escape(normal_value) + r"(?![\d.])", normal_text))
    else:
        normal = normal_text.count(normal_value) if normal_value else 0
    return max(exact, normal)


def _page_hashes(texts: list[str]) -> list[str]:
    return [hashlib.sha256((text or "").encode("utf-8")).hexdigest() for text in texts]


def _signature_evidence(path: Path) -> dict[str, Any]:
    """Inspect AcroForm signatures and common PDF signature markers."""
    evidence: list[str] = []
    status = "safe"
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path), strict=False)
        fields = reader.get_fields() or {}
        for name, field in fields.items():
            if str(field.get("/FT", "")) == "/Sig":
                evidence.append(f"AcroForm 字段 {name}")
        for page_index, page in enumerate(reader.pages, 1):
            for annotation in page.get("/Annots", []) or []:
                try:
                    obj = annotation.get_object()
                    field_type = str(obj.get("/FT", ""))
                    subtype = str(obj.get("/Subtype", ""))
                    if field_type == "/Sig" or subtype == "/Sig":
                        evidence.append(f"第 {page_index} 页签名注释")
                    parent = obj.get("/Parent")
                    if parent is not None and str(parent.get_object().get("/FT", "")) == "/Sig":
                        evidence.append(f"第 {page_index} 页签名字段")
                except Exception:
                    status = "unknown"
    except Exception:
        # A malformed or unsupported PDF must not be declared unsigned.
        status = "unknown"

    # ByteRange is the canonical marker in an embedded PDF signature.  This
    # fallback catches signatures that pypdf cannot fully parse.
    try:
        raw = path.read_bytes()
        if b"/ByteRange" in raw and (b"/Type /Sig" in raw or b"/Sig" in raw):
            evidence.append("PDF 字节流包含 /ByteRange 签名标记")
    except OSError:
        status = "unknown"

    if evidence:
        status = "signed"
    return {"status": status, "detected": status == "signed", "evidence": sorted(set(evidence))}


def _page_visual_signals(text: str, page: Any) -> dict[str, Any]:
    """Return conservative table/chart signals for cross-location checking."""
    text = text or ""
    numeric_count = len(re.findall(r"(?<!\w)-?\d[\d,.]*%?", text))
    table_words = bool(re.search(r"本期|上期|上年同期|变动比例|主要会计数据|营业收入|净利润", text))
    chart_words = bool(re.search(r"(?:图\s*\d|图表|饼图|柱状图|条形图|折线图|趋势图|结构图|分布图)", text))
    try:
        drawings = len(page.get_drawings())
        images = len(page.get_images(full=True))
    except Exception:
        drawings = images = 0
    return {
        "table_candidate": bool(table_words and numeric_count >= 4),
        "chart_candidate": bool(chart_words or images or drawings >= 40),
        "numeric_count": numeric_count,
        "images": images,
        "drawings": drawings,
    }


def _font_coverage(path: Path, page_numbers: Iterable[int], new_values: Iterable[str]) -> dict[str, Any]:
    """Check whether embedded fonts expose the replacement glyphs.

    The check is deliberately conservative.  Without fontTools, or when a PDF
    uses a non-embedded/unsupported font, the result is ``unknown`` and the
    caller may only emit an errata report.
    """
    required = sorted(set("".join(str(value) for value in new_values)))
    if not required:
        return {"status": "unknown", "required_glyphs": [], "missing_glyphs": [],
                "reason": "没有可检查的新值"}
    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except ImportError:
        return {"status": "unknown", "required_glyphs": required, "missing_glyphs": [],
                "reason": "未安装 fontTools，无法确认字体子集字形"}

    fitz = _load_fitz()
    covered: set[str] = set()
    embedded_fonts = 0
    parse_errors: list[str] = []
    with fitz.open(str(path)) as document:
        for number in sorted(set(int(page) for page in page_numbers)):
            if number < 1 or number > len(document):
                continue
            page = document[number - 1]
            for font in page.get_fonts(full=True):
                xref = int(font[0]) if font and font[0] else 0
                if not xref:
                    continue
                try:
                    extracted = document.extract_font(xref)
                    raw = extracted[3] if extracted and len(extracted) > 3 else b""
                    if not raw:
                        continue
                    embedded_fonts += 1
                    font_obj = TTFont(io.BytesIO(raw), lazy=True)
                    for table in font_obj["cmap"].tables:
                        covered.update(chr(code) for code in table.cmap)
                    font_obj.close()
                except Exception as exc:
                    parse_errors.append(f"xref={xref}: {exc}")
    if not embedded_fonts:
        return {"status": "unknown", "required_glyphs": required, "missing_glyphs": [],
                "embedded_fonts": 0, "reason": "未找到可解析的嵌入字体"}
    missing = sorted(set(required) - covered)
    if missing:
        return {"status": "missing", "required_glyphs": required, "covered_glyphs": sorted(covered),
                "missing_glyphs": missing, "embedded_fonts": embedded_fonts,
                "reason": "嵌入字体无法提供全部新值字形"}
    return {"status": "safe", "required_glyphs": required, "covered_glyphs": sorted(covered),
            "missing_glyphs": [], "embedded_fonts": embedded_fonts,
            "parse_errors": parse_errors,
            "reason": "嵌入字体覆盖了待写入字形（仍需版式复核）"}


def preflight_pdf_writeback(
    path: str | Path,
    changes: Iterable[dict[str, Any]],
    *,
    user_confirmed: bool = False,
    allow_writeback: bool = False,
) -> dict[str, Any]:
    """Run all safety checks and return a JSON-serialisable decision.

    ``changes`` uses one-based ``page`` plus ``old``/``new`` (aliases
    ``before``/``after`` and ``old_value``/``new_value`` are accepted).  The
    function never changes the input file.  Repeated values, table/chart
    signals, signatures, missing text, and missing glyph coverage block direct
    write-back.  Unknown font/signature checks downgrade to ``errata_only``.
    """
    pdf_path = Path(path)
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("仅支持 PDF 文件")
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    normalised = [_normalise_change(item) for item in changes]
    if not normalised:
        raise ValueError("至少需要一条 PDF 写回项")

    fitz = _load_fitz()
    with fitz.open(str(pdf_path)) as document:
        texts = [page.get_text("text", sort=True) or "" for page in document]
        signals = [_page_visual_signals(text, page) for text, page in zip(texts, document)]
        pages = len(document)

    hashes = _page_hashes(texts)
    text_pages = sum(len(text.strip()) >= 20 for text in texts)
    text_ratio = text_pages / max(pages, 1)
    text_layer = {
        "pages": text_pages,
        "total_pages": pages,
        "ratio": round(text_ratio, 4),
        "has_text_layer": bool(pages and text_ratio >= 0.5 and all(
            1 <= change["page"] <= pages and len(texts[change["page"] - 1].strip()) >= 20
            for change in normalised
        )),
    }

    occurrence_report: list[dict[str, Any]] = []
    hard_reasons: list[str] = []
    for index, change in enumerate(normalised):
        page = change["page"]
        if page > pages:
            occurrence_report.append({"index": index, **change, "total": 0, "pages": [],
                                      "status": "page_out_of_range"})
            hard_reasons.append(f"第 {page} 页不存在")
            continue
        generated_values = _source_value_variants(change.get("source_value"), change.get("source_unit", ""))
        search_aliases = list(dict.fromkeys(change.get("equivalent_values", []) + generated_values))
        old_matches = [number for number, text in enumerate(texts, 1)
                       if _occurrences(text, change["old"]) > 0]
        counts = {number: _occurrences(texts[number - 1], change["old"]) for number in old_matches}
        alias_matches: dict[str, dict[str, Any]] = {}
        for alias in search_aliases:
            alias_pages = [number for number, text in enumerate(texts, 1)
                           if _occurrences(text, alias) > 0]
            alias_counts = {number: _occurrences(texts[number - 1], alias) for number in alias_pages}
            alias_matches[alias] = {"pages": alias_pages, "counts_by_page": alias_counts,
                                    "total": sum(alias_counts.values())}
        target_count = counts.get(page, 0)
        outside = [number for number in old_matches if number != page]
        alias_outside = sorted({number for item in alias_matches.values()
                                for number in item["pages"] if number != page})
        status = ("unique_target" if old_matches == [page] and target_count == 1 and not alias_outside
                  else "ambiguous_or_missing")
        occurrence_report.append({"index": index, **change, "total": sum(counts.values()),
                                  "pages": old_matches, "counts_by_page": counts,
                                  "equivalent_matches": alias_matches,
                                  "generated_equivalent_values": generated_values,
                                  "target_count": target_count, "outside_target_pages": outside,
                                  "equivalent_outside_target_pages": alias_outside,
                                  "status": status})
        if status != "unique_target":
            hard_reasons.append(f"写回项 {index + 1} 的旧值出现位置不唯一或不存在")
        if signals[page - 1]["table_candidate"] or signals[page - 1]["chart_candidate"]:
            hard_reasons.append(f"第 {page} 页存在表格/图表候选，禁止只改正文")

    signature = _signature_evidence(pdf_path)
    if signature["status"] == "signed":
        hard_reasons.append("检测到数字签名，修改会使签名失效")
    elif signature["status"] == "unknown":
        hard_reasons.append("无法可靠判断数字签名状态")
    if not text_layer["has_text_layer"]:
        hard_reasons.append("未达到文字层覆盖要求，扫描件禁止写回")

    font = _font_coverage(pdf_path, (change["page"] for change in normalised),
                          (change["new"] for change in normalised))
    if font["status"] == "missing":
        hard_reasons.append("待写入数字缺少字体字形")

    source_missing = [index + 1 for index, change in enumerate(normalised)
                      if _decimal_source_value(change.get("source_value")) is None
                      or change.get("source_unit", "").strip() not in _UNIT_FACTORS]
    if source_missing:
        # Without a source fact and unit we cannot automatically search for a
        # scaled rendering in a table.  This is not proof of safety.
        source_reason = ("写回项 " + ", ".join(map(str, source_missing)) +
                         " 未提供可解析的来源事实值和单位，只能生成勘误表")
    else:
        source_reason = ""

    reasons = list(dict.fromkeys(hard_reasons))
    if reasons:
        decision = _DECISION_REJECT
    elif font["status"] != "safe":
        decision = _DECISION_ERRATA
        reasons.append("字体覆盖状态未知，只生成勘误表")
    elif source_reason:
        decision = _DECISION_ERRATA
        reasons.append(source_reason)
    elif not user_confirmed:
        decision = _DECISION_ERRATA
        reasons.append("尚未获得用户对修改版 PDF 的明确确认")
    elif not allow_writeback:
        decision = _DECISION_ERRATA
        reasons.append("调用方未显式打开 PDF 写回开关")
    else:
        decision = _DECISION_WRITE

    return {
        "file": str(pdf_path),
        "pages": pages,
        "text_layer": text_layer,
        "digital_signature": signature,
        "font": font,
        "source_rendering": {
            "required": True,
            "missing_items": source_missing,
            "unit_factors": {key: str(value) for key, value in _UNIT_FACTORS.items()},
            "generated_variant_count": sum(
                len(_source_value_variants(change.get("source_value"), change.get("source_unit", "")))
                for change in normalised
            ),
        },
        "page_hashes": hashes,
        "page_signals": signals,
        "occurrences": occurrence_report,
        "decision": decision,
        "reasons": list(dict.fromkeys(reasons)),
        "policy": {
            "default_output": "errata_only",
            "original_is_never_overwritten": True,
            "requires_user_confirmation": True,
            "writeback_is_emergency_only": True,
        },
    }


def verify_pdf_writeback(
    original_path: str | Path,
    modified_path: str | Path,
    changes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Verify the four post-write invariants without trusting visual output alone."""
    original = Path(original_path)
    modified = Path(modified_path)
    normalised = [_normalise_change(item) for item in changes]
    fitz = _load_fitz()
    with fitz.open(str(original)) as before_doc:
        before_text = [page.get_text("text", sort=True) or "" for page in before_doc]
    with fitz.open(str(modified)) as after_doc:
        after_text = [page.get_text("text", sort=True) or "" for page in after_doc]

    target_pages = {change["page"] for change in normalised}
    page_count_unchanged = len(before_text) == len(after_text)
    before_hashes = _page_hashes(before_text)
    after_hashes = _page_hashes(after_text)
    unchanged_pages = [page for page in range(1, min(len(before_text), len(after_text)) + 1)
                       if page not in target_pages and before_hashes[page - 1] == after_hashes[page - 1]]
    untouched_pages = [page for page in range(1, len(before_text) + 1) if page not in target_pages]
    untouched_hashes_unchanged = untouched_pages == unchanged_pages

    checks: list[dict[str, Any]] = []
    for change in normalised:
        page = change["page"]
        after_page = after_text[page - 1] if page <= len(after_text) else ""
        before_page = before_text[page - 1] if page <= len(before_text) else ""
        old_before = _occurrences(before_page, change["old"])
        old_after = _occurrences(after_page, change["old"])
        new_after = _occurrences(after_page, change["new"])
        checks.append({
            "page": page,
            "old": change["old"],
            "new": change["new"],
            "old_before": old_before,
            "old_after": old_after,
            "new_after": new_after,
            "old_removed_on_target": old_after < old_before,
            "new_independently_extractable": new_after >= 1,
        })
    old_new_checks_pass = all(item["old_removed_on_target"] and item["new_independently_extractable"]
                              for item in checks)
    passed = page_count_unchanged and untouched_hashes_unchanged and old_new_checks_pass
    return {
        "original": str(original),
        "modified": str(modified),
        "page_count": {"original": len(before_text), "modified": len(after_text),
                        "unchanged": page_count_unchanged},
        "untouched_pages": untouched_pages,
        "untouched_hashes_unchanged": untouched_hashes_unchanged,
        "page_hashes": {"original": before_hashes, "modified": after_hashes},
        "changes": checks,
        "passed": passed,
        "requirements": [
            "页数不变",
            "未修改页文本哈希不变",
            "旧值在目标页消失",
            "新值可被独立抽取",
        ],
    }


def build_errata_markdown(
    preflight: dict[str, Any],
    changes: Iterable[dict[str, Any]],
    *,
    title: str = "PDF 勘误表",
) -> str:
    """Build a zero-modification, human-readable errata report."""
    normalised = [_normalise_change(item) for item in changes]
    lines = [f"# {title}", "", "原 PDF 未被修改。本表用于保留原件证据链并记录待确认修正。", "",
             f"- 文件：`{preflight.get('file', '')}`",
             f"- 页数：{preflight.get('pages', 0)}",
             f"- 预检决策：`{preflight.get('decision', _DECISION_ERRATA)}`", ""]
    reasons = preflight.get("reasons") or []
    if reasons:
        lines.append("## 预检说明")
        lines.extend(f"- {reason}" for reason in reasons)
        lines.append("")
    lines.append("## 变更项")
    occurrences = preflight.get("occurrences") or []
    for index, change in enumerate(normalised):
        item = occurrences[index] if index < len(occurrences) else {}
        lines.extend([
            f"### {index + 1}. 第 {change['page']} 页",
            f"- 原值：`{change['old']}`",
            f"- 建议值：`{change['new']}`",
            f"- 原文锚点：{change.get('anchor') or '未提供'}",
            f"- 来源：{change.get('source') or '待确认'}",
            f"- 旧值在全文出现：{item.get('total', '未检查')} 次；页码：{item.get('pages', [])}",
            "",
        ])
    lines.extend(["## 处理建议", "请回到源 Word/Excel 修改后重新导出 PDF。只有在用户明确确认且 PDF 写回预检全部通过时，才考虑生成独立的事后修改版。", ""])
    return "\n".join(lines)


__all__ = ["preflight_pdf_writeback", "verify_pdf_writeback", "build_errata_markdown"]
