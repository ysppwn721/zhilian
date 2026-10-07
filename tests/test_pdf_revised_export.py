import json

import pytest

fitz = pytest.importorskip("fitz")

from zhilian.pdf_revised_export import (available_docx_backends, build_revised_pdf_manifest,
                                        choose_pdf_docx_backend, compare_pdf_visual_fidelity,
                                        verify_revised_pdf)


def _make_pdf(path, pages):
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    document.save(path)
    document.close()


def test_verify_revised_pdf_reopens_output_and_preserves_original(tmp_path):
    original = tmp_path / "original.pdf"
    revised = tmp_path / "revised.pdf"
    _make_pdf(original, ["Original text 125", "Keep unchanged"])
    _make_pdf(revised, ["Revised text 130", "Keep unchanged"])
    before = original.read_bytes()

    result = verify_revised_pdf(original, revised, expected_texts=["Revised text 130"])

    assert result["passed"] is True
    assert result["page_count"]["unchanged"] is True
    assert result["expected_texts"]["Revised text 130"] is True
    assert original.read_bytes() == before


def test_manifest_records_independent_revision_and_hashes(tmp_path):
    original = tmp_path / "original.pdf"
    revised = tmp_path / "revised.pdf"
    _make_pdf(original, ["Original text"])
    _make_pdf(revised, ["Revised text"])
    verification = verify_revised_pdf(original, revised)
    manifest_path = tmp_path / "revision.json"

    manifest = build_revised_pdf_manifest(
        original,
        {"pdf": str(revised), "docx": "edited.docx", "backend": "microsoft_word"},
        verification,
        manifest_path,
    )

    assert manifest["original_is_read_only"] is True
    assert manifest["original_pdf_sha256"]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["verification"]["passed"] is True


def test_backend_capability_detection_is_explicit(monkeypatch):
    backends = available_docx_backends()
    assert set(("native", "pdf2docx", "microsoft_word", "libreoffice")) <= set(backends)
    assert backends["native"]["available"] is True
    monkeypatch.setattr("zhilian.pdf_revised_export.available_docx_backends",
                        lambda: {"native": {"available": True}, "pdf2docx": {"available": False}})
    assert choose_pdf_docx_backend("auto") == "native"


def test_visual_fidelity_gate_passes_for_identical_render_and_writes_report(tmp_path):
    original = tmp_path / "original.pdf"
    revised = tmp_path / "revised.pdf"
    _make_pdf(original, ["Same text"])
    original_bytes = original.read_bytes()
    revised.write_bytes(original_bytes)

    report = compare_pdf_visual_fidelity(original, revised, dpi=72)

    assert report["passed"] is True
    assert report["changed_page_count"] == 0
    assert report["pages"][0]["text_exact"] is True


def test_visual_fidelity_gate_reports_changed_page_and_bbox(tmp_path):
    original = tmp_path / "original.pdf"
    revised = tmp_path / "revised.pdf"
    _make_pdf(original, ["Original text"])
    _make_pdf(revised, ["Changed text"])

    report = compare_pdf_visual_fidelity(original, revised, dpi=72)

    assert report["passed"] is False
    assert report["status"] == "needs_visual_review"
    assert report["changed_page_count"] == 1
    assert report["pages"][0]["bbox"] is not None
    assert report["pages"][0]["text_exact"] is False
