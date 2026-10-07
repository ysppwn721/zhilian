"""Experimental WPS-like PDF text replacement using a visual overlay.

This module is deliberately separate from the production write-back policy.
It demonstrates the common desktop-editor technique: locate a text span,
cover the old glyphs with a redaction rectangle, and paint the replacement at
the same coordinates in a new PDF.  It does not provide paragraph reflow,
table semantics, signature preservation, or OCR editing.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def _fitz():
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    return fitz


def _rgb_from_color(value: int | None) -> tuple[float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0)
    return ((value >> 16 & 255) / 255, (value >> 8 & 255) / 255, (value & 255) / 255)


def _find_span(page: Any, old: str, rect: Any) -> dict[str, Any]:
    """Find style metadata for a search rectangle, when the text layer exposes it."""
    try:
        blocks = page.get_text("dict", sort=True).get("blocks", [])
    except Exception:
        return {}
    best: dict[str, Any] = {}
    best_area = math.inf
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                bbox = span.get("bbox")
                if not text or not bbox:
                    continue
                span_rect = page.rect.__class__(bbox)
                overlap = span_rect & rect
                if overlap.is_empty:
                    continue
                if old in text or overlap.get_area() < best_area:
                    best = span
                    best_area = overlap.get_area()
                    if old in text:
                        return best
    return best


def overlay_replace_pdf(
    input_path: str | Path,
    output_path: str | Path,
    *,
    page_number: int,
    old: str,
    new: str,
    occurrence: int = 0,
    fill: tuple[float, float, float] = (1.0, 1.0, 1.0),
    font_file: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new PDF with one visual text replacement.

    This is an experiment, not a safe production writer.  It requires exactly
    one matching occurrence on the selected page unless ``occurrence`` chooses
    one of several matches.  The original file is never opened for writing.
    """
    source = Path(input_path)
    target = Path(output_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if page_number < 1:
        raise ValueError("page_number 从 1 开始")
    if not old or not new:
        raise ValueError("old 和 new 不能为空")
    target.parent.mkdir(parents=True, exist_ok=True)

    fitz = _fitz()
    with fitz.open(str(source)) as document:
        if page_number > len(document):
            raise ValueError(f"PDF 不存在第 {page_number} 页")
        page = document[page_number - 1]
        matches = page.search_for(old)
        if not matches:
            raise ValueError(f"第 {page_number} 页找不到旧值 {old!r}")
        if occurrence < 0 or occurrence >= len(matches):
            raise ValueError(f"occurrence 超出范围：共有 {len(matches)} 处")
        rect = matches[occurrence]
        span = _find_span(page, old, rect)
        fontsize = float(span.get("size") or max(rect.height * 0.8, 6.0))
        color = _rgb_from_color(span.get("color"))
        old_width = float(rect.width)
        # This estimate is intentionally conservative; a replacement wider
        # than the old box may collide with the following PDF text object.
        estimated_width = max(len(new), 1) / max(len(old), 1) * old_width
        layout_risk = estimated_width > old_width * 1.05

        # Redaction permanently removes the old glyphs from the output page,
        # after which a replacement is painted at the same baseline region.
        page.add_redact_annot(rect, fill=fill)
        page.apply_redactions()
        kwargs: dict[str, Any] = {
            "fontsize": fontsize,
            "color": color,
            "overlay": True,
        }
        if font_file:
            kwargs["fontfile"] = str(font_file)
        # insert_text uses a baseline point.  y1 - 1.5 is a good approximation
        # for Latin text and keeps this experiment deterministic.
        baseline = rect.y1 - max(1.5, fontsize * 0.12)
        page.insert_text((rect.x0, baseline), new, **kwargs)
        document.save(str(target), garbage=4, deflate=True)

    return {
        "mode": "visual_overlay",
        "input": str(source),
        "output": str(target),
        "page": page_number,
        "old": old,
        "new": new,
        "occurrence": occurrence,
        "old_rect": {"x0": rect.x0, "y0": rect.y0, "x1": rect.x1, "y1": rect.y1},
        "font": span.get("font"),
        "fontsize": fontsize,
        "layout_risk": layout_risk,
        "warning": "实验性视觉覆盖：不保证段落重排、表格一致性、签名和原字体完全保真",
    }


__all__ = ["overlay_replace_pdf"]
