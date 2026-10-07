import pytest

fitz = pytest.importorskip("fitz")

from zhilian import pdf_writeback


def _make_pdf(path, pages):
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    document.save(path)
    document.close()


def test_preflight_defaults_to_errata_when_font_coverage_is_unknown(tmp_path):
    source = tmp_path / "source.pdf"
    _make_pdf(source, ["Revenue for current period is 125 CNY."])

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "125", "new": "130", "anchor": "营收"}],
    )

    assert result["decision"] == "errata_only"
    assert result["text_layer"]["has_text_layer"] is True
    assert result["occurrences"][0]["status"] == "unique_target"
    assert result["font"]["status"] == "unknown"
    assert result["policy"]["original_is_never_overwritten"] is True


def test_duplicate_old_value_rejects_body_only_writeback(tmp_path):
    source = tmp_path / "duplicate.pdf"
    _make_pdf(source, ["Revenue for current period is 125 CNY.", "Table Revenue 125 100 25 25%"])

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "125", "new": "130"}],
    )

    assert result["decision"] == "reject"
    assert any("位置不唯一" in reason for reason in result["reasons"])


def test_equivalent_table_value_also_rejects_body_only_writeback(tmp_path):
    source = tmp_path / "equivalent.pdf"
    _make_pdf(source, ["Revenue 6.04 CNY hundred million.",
                       "Table Revenue 604,539,544.17 CNY."])

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "6.04", "new": "5.80",
          "equivalent_values": ["604,539,544.17"]}],
    )

    assert result["decision"] == "reject"
    assert result["occurrences"][0]["equivalent_outside_target_pages"] == [2]


def test_source_fact_automatically_finds_scaled_and_rounded_table_value(tmp_path):
    source = tmp_path / "auto-equivalent.pdf"
    _make_pdf(source, ["Revenue is 6.04 billion CNY.",
                       "Table Revenue 604,539,544.17 CNY."])

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "6.04", "new": "5.80",
          "source_value": 604539544.17, "source_unit": "元"}],
    )

    assert result["decision"] == "reject"
    assert result["source_rendering"]["generated_variant_count"] > 0
    assert result["occurrences"][0]["equivalent_outside_target_pages"] == [2]


def test_scanned_or_empty_pdf_rejects_writeback(tmp_path):
    source = tmp_path / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(source)
    document.close()

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "125", "new": "130"}],
    )

    assert result["decision"] == "reject"
    assert any("文字层" in reason for reason in result["reasons"])


def test_table_signal_rejects_body_only_writeback(tmp_path, monkeypatch):
    source = tmp_path / "table.pdf"
    _make_pdf(source, ["Revenue table 125 100 25% Profit 8 6 33%"])
    monkeypatch.setattr(pdf_writeback, "_page_visual_signals", lambda *args, **kwargs: {
        "table_candidate": True, "chart_candidate": False, "numeric_count": 6,
        "images": 0, "drawings": 0,
    })

    result = pdf_writeback.preflight_pdf_writeback(
        source,
        [{"page": 1, "old": "125", "new": "130"}],
    )

    assert result["decision"] == "reject"
    assert any("表格/图表" in reason for reason in result["reasons"])


def test_writeback_requires_explicit_user_confirmation_and_switch(tmp_path, monkeypatch):
    source = tmp_path / "confirmed.pdf"
    _make_pdf(source, ["Revenue for current period is 125 CNY."])
    monkeypatch.setattr(pdf_writeback, "_font_coverage", lambda *args, **kwargs: {
        "status": "safe", "required_glyphs": ["1", "3", "0"], "missing_glyphs": []
    })
    monkeypatch.setattr(pdf_writeback, "_signature_evidence", lambda *args, **kwargs: {
        "status": "safe", "detected": False, "evidence": []
    })

    pending = pdf_writeback.preflight_pdf_writeback(
        source, [{"page": 1, "old": "125", "new": "130", "source_value": 125, "source_unit": "元"}], user_confirmed=False,
        allow_writeback=True,
    )
    assert pending["decision"] == "errata_only"

    allowed = pdf_writeback.preflight_pdf_writeback(
        source, [{"page": 1, "old": "125", "new": "130", "source_value": 125, "source_unit": "元"}], user_confirmed=True,
        allow_writeback=True,
    )
    assert allowed["decision"] == "writeback_allowed"


def test_verify_requires_page_count_and_untouched_page_hashes(tmp_path):
    original = tmp_path / "original.pdf"
    modified = tmp_path / "modified.pdf"
    _make_pdf(original, ["Revenue for current period is 125 CNY.", "Second page stays unchanged."])
    _make_pdf(modified, ["Revenue for current period is 130 CNY.", "Second page stays unchanged."])

    result = pdf_writeback.verify_pdf_writeback(
        original, modified, [{"page": 1, "old": "125", "new": "130"}]
    )

    assert result["passed"] is True
    assert result["page_count"]["unchanged"] is True
    assert result["untouched_hashes_unchanged"] is True
    assert result["changes"][0]["new_independently_extractable"] is True


def test_errata_markdown_keeps_original_untouched(tmp_path):
    source = tmp_path / "source.pdf"
    _make_pdf(source, ["Revenue for current period is 125 CNY."])
    preflight = pdf_writeback.preflight_pdf_writeback(
        source, [{"page": 1, "old": "125", "new": "130"}]
    )
    markdown = pdf_writeback.build_errata_markdown(
        preflight, [{"page": 1, "old": "125", "new": "130", "source": "事实表 A1"}]
    )
    assert "原 PDF 未被修改" in markdown
    assert "125" in markdown and "130" in markdown
    assert source.exists()

