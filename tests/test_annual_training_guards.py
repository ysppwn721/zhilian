"""Prevent evaluation reuse and company leakage in optional training scripts."""
import json

import pytest

from 答辩评测.build_annual_report_text_pairs import audit_training_sources, split_groups
from 答辩评测.train_reranker import audit_manifests


def test_all_years_of_company_stay_in_same_split():
    groups = [f"annual-pdf-{company}-{year}" for company in
              ("000001", "000002", "000003", "000004", "000005", "000006", "000007")
              for year in (2022, 2023)]
    splits = split_groups(groups)
    for company in ("000001", "000002", "000003", "000004", "000005", "000006", "000007"):
        assert splits[f"annual-pdf-{company}-2022"] == splits[f"annual-pdf-{company}-2023"]
    assert set(splits.values()) == {"train", "dev", "test"}
    assert splits == split_groups(list(reversed(groups)))


def test_insufficient_independent_companies_are_refused():
    with pytest.raises(ValueError, match="three independent companies"):
        split_groups(["annual-pdf-000001-2022", "annual-pdf-000001-2023"])


def test_three_companies_produce_three_nonempty_splits():
    splits = split_groups([f"annual-pdf-{company}-2023" for company in
                           ("000001", "000002", "000003")])
    assert set(splits.values()) == {"train", "dev", "test"}


def test_frozen_company_is_excluded_even_in_a_new_year():
    with pytest.raises(ValueError, match="Frozen evaluation companies"):
        audit_training_sources([{"group_id": "annual-pdf-000001-2024"}],
                               [{"group_id": "external-pdf-000001-2023-abc"}])


def test_independent_company_is_allowed():
    audit_training_sources([{"group_id": "annual-pdf-000002-2024"}],
                           [{"group_id": "external-pdf-000001-2023-abc"}])


def test_pending_label_audit_blocks_training(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"ready_for_training": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="not approved for training"):
        audit_manifests([tmp_path / "train.jsonl"])


def test_legacy_manifests_without_an_explicit_block_are_readable(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    audit_manifests([tmp_path / "train.jsonl"])
