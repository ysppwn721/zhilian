from 答辩评测.triage_merged_candidates import triage


def base(**overrides):
    row = {
        "scope": "合并",
        "unit": "元",
        "three_value_check": "consistent",
        "match_level": "exact",
        "claim_text": "公司营业收入为100元",
    }
    row.update(overrides)
    return row


def test_inconsistent_three_value_is_rejected():
    status, reasons = triage(base(three_value_check="inconsistent"))
    assert status == "reject_or_abstain"
    assert "three_value_check_failed" in reasons


def test_missing_scope_requires_review():
    status, reasons = triage(base(scope="未标明"))
    assert status == "needs_scope_unit_review"
    assert "scope_not_explicit" in reasons


def test_complete_evidence_still_needs_label():
    status, reasons = triage(base())
    assert status == "evidence_complete_pending_label"
    assert reasons == []
