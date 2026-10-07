"""Create and verify an independent revised PDF from an edited DOCX.

This is intentionally separate from PDF ingestion.  The source PDF is never
overwritten: a user-approved repaired DOCX is exported to a new PDF, then the
new PDF is reopened with PyMuPDF and compared with the original provenance
record.  Microsoft Word is preferred on Windows because it is the same layout
engine users use to edit the DOCX; LibreOffice is the portable fallback when
available on Linux/macOS.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_libreoffice() -> str | None:
    for name in ("libreoffice", "soffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def available_docx_backends() -> dict[str, dict[str, Any]]:
    """Report conversion backends without attempting to start an office app."""
    word = False
    if os.name == "nt":
        word = any(Path(candidate).is_file() for candidate in (
            os.environ.get("PROGRAMFILES", "") + r"\Microsoft Office\root\Office16\WINWORD.EXE",
            os.environ.get("PROGRAMFILES(X86)", "") + r"\Microsoft Office\root\Office16\WINWORD.EXE",
        )) or importlib.util.find_spec("win32com.client") is not None
    return {
        "pdf2docx": {"available": importlib.util.find_spec("pdf2docx") is not None,
                     "role": "born-digital PDF 转可编辑 DOCX"},
        "native": {"available": True, "role": "坐标感知抽取与审计锚点"},
        "microsoft_word": {"available": word, "role": "DOCX 导出修订版 PDF"},
        "libreoffice": {"available": bool(_find_libreoffice()),
                        "role": "跨平台 DOCX 导出修订版 PDF"},
    }


def choose_pdf_docx_backend(requested: str = "auto") -> str:
    """Choose the editable-DOCX backend while keeping the choice auditable."""
    selected = (requested or "auto").strip().lower()
    if selected not in {"auto", "native", "pdf2docx"}:
        raise ValueError("docx_backend 仅支持 auto、native 或 pdf2docx")
    if selected == "auto":
        # pdf2docx has the best page/table fidelity on born-digital reports;
        # native remains the guaranteed fallback when optional deps are absent.
        selected = "pdf2docx" if available_docx_backends()["pdf2docx"]["available"] else "native"
    if selected == "pdf2docx" and not available_docx_backends()["pdf2docx"]["available"]:
        raise RuntimeError("pdf2docx 后端未安装，请安装 requirements-pdf.txt")
    return selected


def _export_with_word(docx: Path, output_pdf: Path) -> str:
    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Windows Word 导出需要 pywin32") from exc
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(docx), ReadOnly=True, AddToRecentFiles=False)
        # 17 = wdExportFormatPDF; 0/1 select print/quality settings used by
        # Word's normal Save as PDF path.
        document.ExportAsFixedFormat(str(output_pdf), 17, False, 0, 0, 1, 1, 0,
                                     True, False, 0, True, True, False)
        return "microsoft_word"
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass


def _export_with_libreoffice(docx: Path, output_pdf: Path, binary: str) -> str:
    with tempfile.TemporaryDirectory(prefix="zhilian-pdf-") as temp:
        completed = subprocess.run(
            [binary, "--headless", "--convert-to", "pdf", "--outdir", temp, str(docx)],
            capture_output=True, text=True, timeout=180,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "LibreOffice 导出失败")
        generated = Path(temp) / f"{docx.stem}.pdf"
        if not generated.is_file():
            raise RuntimeError("LibreOffice 未生成 PDF")
        shutil.copyfile(generated, output_pdf)
    return "libreoffice"


def export_pdf_to_editable_docx(pdf_path: str | Path, output_docx: str | Path,
                                *, start: int = 0, end: int | None = None) -> dict[str, Any]:
    """Use the optional born-digital converter as an alternate DOCX backend.

    This function is deliberately opt-in.  The result is editable and often
    closer to the source layout than plain text reconstruction, but it still
    needs page-count and visual verification before it can be a delivery file.
    ``start`` is zero-based and ``end`` follows pdf2docx's exclusive bound.
    """
    source = Path(pdf_path).resolve()
    target = Path(output_docx).resolve()
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError("需要存在的 PDF 文件")
    if target.suffix.lower() != ".docx":
        raise ValueError("输出文件必须是 DOCX")
    if start < 0 or (end is not None and end <= start):
        raise ValueError("PDF 页码范围无效")
    try:
        from pdf2docx import Converter  # type: ignore
    except ImportError as exc:
        raise RuntimeError("可选后端未安装，请安装 requirements-pdf.txt") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    converter = Converter(str(source))
    try:
        converter.convert(str(target), start=start, end=end, multi_processing=False)
    finally:
        converter.close()
    if not target.is_file():
        raise RuntimeError("PDF 转 DOCX 未生成输出文件")
    return {"pdf": str(source), "docx": str(target), "backend": "pdf2docx",
            "start_page": start, "end_page": end, "pdf_sha256": _sha256(source),
            "docx_sha256": _sha256(target), "original_is_read_only": True,
            "requires_visual_review": True}


def export_docx_to_pdf(docx_path: str | Path, output_pdf: str | Path, *, backend: str = "auto") -> dict[str, Any]:
    """Export an edited DOCX to a new PDF without touching the input files."""
    docx = Path(docx_path).resolve()
    target = Path(output_pdf).resolve()
    if docx.suffix.lower() != ".docx" or not docx.is_file():
        raise ValueError("需要存在的 DOCX 文件")
    if target.suffix.lower() != ".pdf":
        raise ValueError("输出文件必须是 PDF")
    target.parent.mkdir(parents=True, exist_ok=True)
    selected = backend.lower().strip()
    if selected not in {"auto", "word", "libreoffice"}:
        raise ValueError("backend 仅支持 auto、word 或 libreoffice")
    if selected in {"auto", "word"} and os.name == "nt":
        try:
            engine = _export_with_word(docx, target)
        except Exception:
            if selected == "word":
                raise
            binary = _find_libreoffice()
            if not binary:
                raise
            engine = _export_with_libreoffice(docx, target, binary)
    else:
        binary = _find_libreoffice()
        if not binary:
            raise RuntimeError("未找到 LibreOffice；Linux/macOS 请安装 libreoffice 后重试")
        engine = _export_with_libreoffice(docx, target, binary)
    return {"docx": str(docx), "pdf": str(target), "backend": engine,
            "available_backends": available_docx_backends(),
            "docx_sha256": _sha256(docx), "pdf_sha256": _sha256(target),
            "original_is_read_only": True}


def verify_revised_pdf(original_pdf: str | Path, revised_pdf: str | Path,
                       *, expected_texts: list[str] | None = None) -> dict[str, Any]:
    """Reopen a revised PDF and report conservative independent checks."""
    original = Path(original_pdf).resolve()
    revised = Path(revised_pdf).resolve()
    if not original.is_file() or not revised.is_file():
        raise FileNotFoundError("原始 PDF 或修订版 PDF 不存在")
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    with fitz.open(str(original)) as before, fitz.open(str(revised)) as after:
        before_text = [page.get_text("text", sort=True) or "" for page in before]
        after_text = [page.get_text("text", sort=True) or "" for page in after]
        required = expected_texts or []
        found = {text: any(text in page for page in after_text) for text in required}
        page_count_unchanged = len(before_text) == len(after_text)
        expected_ok = all(found.values()) if required else True
        return {
            "original": str(original), "revised": str(revised),
            "original_sha256": _sha256(original), "revised_sha256": _sha256(revised),
            "page_count": {"original": len(before_text), "revised": len(after_text),
                            "unchanged": page_count_unchanged},
            "text_layer": {"original_chars": sum(map(len, before_text)),
                           "revised_chars": sum(map(len, after_text)),
                           "revised_nonempty_pages": sum(bool(text.strip()) for text in after_text)},
            "expected_texts": found,
            # A reconstructed DOCX can be readable while still changing page
            # boundaries.  Treat that as unverified so callers cannot present
            # a visually different PDF as a faithful repair.
            "passed": bool(len(after_text) and page_count_unchanged and expected_ok),
            "status": ("passed" if len(after_text) and page_count_unchanged and expected_ok
                       else "needs_visual_review"),
            "original_is_read_only": True,
        }


def compare_pdf_visual_fidelity(original_pdf: str | Path, revised_pdf: str | Path,
                                *, diff_dir: str | Path | None = None,
                                dpi: int = 96, pixel_tolerance: int = 0) -> dict[str, Any]:
    """Compare rendered pages and report every detectable visual difference.

    A DOCX round trip can preserve extracted text while changing line breaks,
    tables, fonts or coordinates.  Page count alone cannot detect those
    changes, so this check renders both PDFs at the same DPI and compares the
    RGB pixels.  The default tolerance is deliberately strict (zero): any
    changed pixel makes the result ``needs_visual_review``.  A diff image and
    bounding box are written for every changed page when ``diff_dir`` is set.
    This is a gate for user review, not a claim that raster equality proves
    semantic correctness.
    """
    original = Path(original_pdf).resolve()
    revised = Path(revised_pdf).resolve()
    if not original.is_file() or not revised.is_file():
        raise FileNotFoundError("原始 PDF 或修订版 PDF 不存在")
    if dpi < 36 or dpi > 300:
        raise ValueError("dpi 应在 36 到 300 之间")
    if pixel_tolerance < 0 or pixel_tolerance > 255:
        raise ValueError("pixel_tolerance 应在 0 到 255 之间")
    try:
        import numpy as np  # type: ignore
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import numpy as np  # type: ignore
            import fitz  # type: ignore
        except ImportError as exc:
            raise RuntimeError("视觉比对需要 pymupdf 和 numpy") from exc

    output = Path(diff_dir).resolve() if diff_dir else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    pages: list[dict[str, Any]] = []
    with fitz.open(str(original)) as before, fitz.open(str(revised)) as after:
        original_page_count = len(before)
        revised_page_count = len(after)
        common = min(original_page_count, revised_page_count)
        for index in range(common):
            left = before[index].get_pixmap(matrix=matrix, alpha=False)
            right = after[index].get_pixmap(matrix=matrix, alpha=False)
            left_array = np.frombuffer(left.samples, dtype=np.uint8).reshape(left.height, left.width, left.n)
            right_array = np.frombuffer(right.samples, dtype=np.uint8).reshape(right.height, right.width, right.n)
            same_shape = left_array.shape == right_array.shape
            if same_shape:
                channels = min(left_array.shape[2], right_array.shape[2], 3)
                delta = np.abs(left_array[:, :, :channels].astype(np.int16) -
                               right_array[:, :, :channels].astype(np.int16)).max(axis=2)
                changed = delta > pixel_tolerance
                changed_pixels = int(changed.sum())
                total_pixels = int(changed.size)
                ratio = changed_pixels / max(total_pixels, 1)
                ys, xs = np.where(changed)
                bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if changed_pixels else None
            else:
                changed_pixels = None
                total_pixels = None
                ratio = 1.0
                bbox = [0, 0, max(left.width, right.width), max(left.height, right.height)]
            left_text = before[index].get_text("text", sort=True) or ""
            right_text = after[index].get_text("text", sort=True) or ""
            row = {
                "page": index + 1,
                "same_render_size": same_shape,
                "original_render_size": [left.width, left.height],
                "revised_render_size": [right.width, right.height],
                "changed_pixels": changed_pixels,
                "total_pixels": total_pixels,
                "pixel_diff_ratio": round(ratio, 8),
                "bbox": bbox,
                "text_exact": left_text == right_text,
                "original_text_sha256": hashlib.sha256(left_text.encode("utf-8")).hexdigest(),
                "revised_text_sha256": hashlib.sha256(right_text.encode("utf-8")).hexdigest(),
                "status": "identical" if same_shape and changed_pixels == 0 and left_text == right_text else "changed",
            }
            if output and row["status"] != "identical":
                # Make the visual discrepancy inspectable without altering either PDF.
                right.save(output / f"page-{index + 1:04d}-revised.png")
                if same_shape:
                    from PIL import Image
                    diff = np.zeros_like(left_array[:, :, :3])
                    diff[changed] = [220, 40, 40]
                    Image.fromarray(diff, mode="RGB").save(output / f"page-{index + 1:04d}-diff.png")
                row["revised_preview"] = str(output / f"page-{index + 1:04d}-revised.png")
                row["diff_preview"] = str(output / f"page-{index + 1:04d}-diff.png") if same_shape else None
            pages.append(row)
        for index in range(common, original_page_count):
            pages.append({"page": index + 1, "status": "missing_in_revised", "bbox": None})
        for index in range(common, revised_page_count):
            pages.append({"page": index + 1, "status": "extra_in_revised", "bbox": None})
    passed = bool(pages) and original_page_count == revised_page_count and all(page.get("status") == "identical" for page in pages)
    return {
        "original": str(original),
        "revised": str(revised),
        "dpi": dpi,
        "pixel_tolerance": pixel_tolerance,
        "page_count": {"original": original_page_count, "revised": revised_page_count,
                        "unchanged": original_page_count == revised_page_count},
        "changed_page_count": sum(page.get("status") != "identical" for page in pages),
        "passed": passed,
        "status": "passed" if passed else "needs_visual_review",
        "pages": pages,
        "original_is_read_only": True,
    }


def build_revised_pdf_manifest(original_pdf: str | Path, docx_pdf: dict[str, Any],
                               verification: dict[str, Any], output: str | Path) -> dict[str, Any]:
    """Write an auditable manifest alongside a revised PDF."""
    original = Path(original_pdf).resolve()
    manifest = {
        "original_pdf": str(original), "original_pdf_sha256": _sha256(original),
        "revised_pdf": docx_pdf.get("pdf"), "repaired_docx": docx_pdf.get("docx"),
        "export_backend": docx_pdf.get("backend"),
        "verification": verification,
        "original_is_read_only": True,
        "delivery_note": "修订版 PDF 由确认后的 Word 重新导出，不覆盖原始 PDF；签名和原件效力不随修订版继承。",
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


__all__ = ["available_docx_backends", "choose_pdf_docx_backend",
           "export_pdf_to_editable_docx", "export_docx_to_pdf",
           "verify_revised_pdf", "compare_pdf_visual_fidelity",
           "build_revised_pdf_manifest"]
