from zhilian.document_profile import detect
from zhilian import reranker


def test_annual_profile_requires_name_and_report_signals():
    result = detect(
        [{'name': '某公司2024年年度报告.docx', 'kind': 'docx'}],
        [{'text': '重要提示：本报告期公司营业收入增长。合并资产负债表如下。'}],
    )
    assert result['profile'] == 'annual'
    assert result['annual_name_hits'] == 1
    assert 'accounting_statement' in result['reasons']


def test_generic_document_does_not_select_annual_model():
    result = detect(
        [{'name': '项目说明.docx', 'kind': 'docx'}],
        [{'text': '本项目收入为100万元，下一步继续实施。'}],
    )
    assert result['profile'] == 'default'


def test_normalized_listed_company_pdf_name_selects_annual_profile():
    result = detect(
        [{'name': '000543_2018_皖能电力.pdf', 'kind': 'pdf'}],
        [{'text': '报告期内公司实现营业收入；审计报告确认归属于上市公司股东的净利润。'}],
    )
    assert result['profile'] == 'annual'
    assert result['financial_pdf_name_hits'] == 1


def test_annual_reranker_profile_can_be_selected_without_global_switch(monkeypatch, tmp_path):
    model_dir = tmp_path / 'annual'
    model_dir.mkdir()
    (model_dir / 'model_fp32.onnx').write_bytes(b'placeholder')
    (model_dir / 'tokenizer.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('ZHILIAN_ANNUAL_RERANKER_PATH', str(model_dir))
    monkeypatch.setenv('ZHILIAN_ANNUAL_RERANKER_ENABLED', '1')
    status = reranker.status('annual')
    assert status['profile'] == 'annual'
    assert status['enabled'] is True
