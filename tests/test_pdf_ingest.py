from zhilian.pdf_ingest import (_calculated_change, _header_columns,
                                _quarantine_conflicting_facts,
                                _repair_split_numeric_cells, to_number)


def test_pdf_number_parser_handles_thousands_and_percent():
    assert to_number("1,032,869,576.20") == 1032869576.20
    assert to_number("-97.29%") == -97.29
    assert to_number("—") is None


def test_pdf_header_prefers_change_percentage_over_change_amount():
    rows = [
        ["", "2022年", "2021年", "变动金额", "变动比例（%）"],
        ["营业收入", "100", "80", "20", "25.00%"],
    ]
    current, prior, change = _header_columns(rows[:1])
    assert (current, prior, change) == (1, 2, 4)


def test_pdf_header_accepts_year_without_suffix_before_change_column():
    rows = [["", "2022年", "2021", "本年比上年增减", "2020年"],
            ["营业收入", "100", "80", "25.00%", "70"]]
    assert _header_columns(rows[:1]) == (1, 2, 3)


def test_pdf_repair_keeps_table_column_positions():
    row = ["资产总计", "1,", "032,869,576.20", "854,435,427.64", "20.88%"]
    fixed = _repair_split_numeric_cells(row)
    assert fixed[1] == ""
    assert fixed[2] == "1,032,869,576.20"
    assert fixed[3] == "854,435,427.64"


def test_pdf_ratio_metric_uses_percentage_point_change():
    assert _calculated_change('研发投入占营业收入比例', 3.84, 3.15, '3.84%', '3.15%') == 0.69
    assert _calculated_change('营业收入', 125, 100, '125', '100') == 25


def test_conflicting_same_page_facts_are_quarantined():
    facts = [
        {"id": "a", "source_page": 70, "metric": "营业收入", "period": "本期",
         "value": 16_456_919.36, "unit": "元", "source_table": 1, "source_row": 3},
        {"id": "b", "source_page": 70, "metric": "营业收入", "period": "本期",
         "value": 13_416_456_919.36, "unit": "元", "source_table": 2, "source_row": 2},
        {"id": "c", "source_page": 70, "metric": "营业成本", "period": "本期",
         "value": 12_610_339_015.64, "unit": "元", "source_table": 2, "source_row": 8},
    ]

    accepted, conflicts = _quarantine_conflicting_facts(facts)

    assert [item["id"] for item in accepted] == ["c"]
    assert len(conflicts) == 1
    assert conflicts[0]["metric"] == "营业收入"
    assert {item["value"] for item in conflicts[0]["candidates"]} == {
        16_456_919.36, 13_416_456_919.36,
    }
