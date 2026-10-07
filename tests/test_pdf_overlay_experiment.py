import pytest

fitz = pytest.importorskip("fitz")

from zhilian.pdf_overlay_experiment import overlay_replace_pdf
from zhilian.pdf_writeback import verify_pdf_writeback


def _make_pdf(path):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Revenue for current period is 125 CNY.", fontsize=12)
    document.save(path)
    document.close()


def test_visual_overlay_writes_a_new_pdf_and_replaces_text(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "overlay.pdf"
    _make_pdf(source)

    result = overlay_replace_pdf(source, output, page_number=1, old="125", new="130")

    assert result["mode"] == "visual_overlay"
    assert output.exists()
    with fitz.open(output) as document:
        text = document[0].get_text("text")
    assert "130" in text
    assert "125" not in text

    verified = verify_pdf_writeback(source, output, [{"page": 1, "old": "125", "new": "130"}])
    assert verified["passed"] is True


def test_visual_overlay_rejects_missing_text(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "overlay.pdf"
    _make_pdf(source)
    with pytest.raises(ValueError, match="找不到"):
        overlay_replace_pdf(source, output, page_number=1, old="999", new="130")
