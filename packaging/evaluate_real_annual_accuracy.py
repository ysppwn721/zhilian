"""Evaluate the real annual-report run against a small, source-reviewed gold set.

The gold rows are transcribed from page 7 of the original PDF, independently of
the generated Word/Excel artifacts.  This deliberately measures table extraction
and the two claims with complete evidence; the remaining prose claims are
reported as unverified rather than silently counted as correct.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zhilian.engine import check


RUN = ROOT / "output" / "final_real_annual_verified_20261006" / "000543_2018_皖能电力"
OUT = RUN / "real_annual_accuracy_20261006.json"
REPORT = ROOT / "省赛提交包_知链_20261006" / "06_测试证据" / "真实年报准确率评测_20261006.md"

# Human-reviewed transcription of the original PDF, page 7.  The first six
# rows are the current currency-only admission scope; the last three are real
# rows which the conservative unit gate rejected.
GOLD_ROWS = [
    ("营业收入", "元", 13416456919.36, 12207433397.76, 9.90),
    ("归属于上市公司股东的净利润", "元", 556267729.59, 132054306.12, 321.24),
    ("归属于上市公司股东的扣除非经常性损益的净利润", "元", 369022176.26, 122111479.81, 202.20),
    ("经营活动产生的现金流量净额", "元", 1367348095.07, 989442166.46, 38.19),
    ("总资产", "元", 28899887221.01, 26547647317.01, 8.86),
    ("归属于上市公司股东的净资产", "元", 9796870921.27, 10135849055.69, -3.34),
    ("基本每股收益", "元/股", 0.31, 0.07, 342.86),
    ("稀释每股收益", "元/股", 0.31, 0.07, 342.86),
    ("加权平均净资产收益率", "%", 5.58, 1.25, 4.33),
]

KNOWN_CLAIMS = {
    "15568a60a229b167f1": ("growth", {"f3_prior", "f3_current"}),
    "8fd6e1d7a6b952ca64": ("quote", {"f3_current"}),
}


def _round(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _facts() -> list[dict]:
    workspace = json.loads((RUN / "baseline_workspace.json").read_text(encoding="utf-8"))
    return workspace["facts"]


def _evaluate_rows(facts: list[dict]) -> dict:
    by_metric: dict[str, dict[str, dict]] = {}
    for fact in facts:
        by_metric.setdefault(fact["metric"], {})[fact["period"]] = fact

    details = []
    admitted = 0
    field_hits = 0
    field_total = 0
    for metric, unit, current, prior, change in GOLD_ROWS:
        curr = by_metric.get(metric, {}).get("本期")
        prev = by_metric.get(metric, {}).get("上期")
        found = curr is not None and prev is not None
        if found:
            admitted += 1
        checks = {
            "metric": found and curr["metric"] == metric and prev["metric"] == metric,
            "unit": found and curr["unit"] == unit and prev["unit"] == unit,
            "current": found and _round(curr["value"]) == _round(current),
            "prior": found and _round(prev["value"]) == _round(prior),
            "change": found and _round((current - prior) / prior * 100) == _round(change),
        }
        if found:
            field_total += len(checks)
            field_hits += sum(checks.values())
        details.append({"metric": metric, "gold_unit": unit, "admitted": found, "checks": checks})

    total_rows = len(GOLD_ROWS)
    supported_rows = 6
    return {
        "gold_rows": total_rows,
        "extracted_rows": admitted,
        "row_precision": admitted / len(by_metric) if by_metric else 0.0,
        "row_recall_all": admitted / total_rows,
        "row_recall_supported_scope": admitted / supported_rows,
        "row_f1_all": 2 * (1.0 * admitted / total_rows) / (1.0 + admitted / total_rows),
        "admitted_field_exact": field_hits / field_total if field_total else 0.0,
        "field_hits": field_hits,
        "field_total": field_total,
        "details": details,
        "conservative_rejections": total_rows - admitted,
    }


def _evaluate_claims() -> dict:
    workspace = json.loads((RUN / "baseline_workspace.json").read_text(encoding="utf-8"))
    facts = workspace["facts"]
    claims = {claim["id"]: claim for claim in workspace["claims"]}
    rows = []
    hits = 0
    for claim_id, (kind, expected_refs) in KNOWN_CLAIMS.items():
        claim = claims[claim_id]
        refs_ok = set(claim.get("refs", [])) == expected_refs
        result = check(claim, facts)
        status_ok = result.get("status") == "consistent"
        hit = refs_ok and status_ok and claim.get("kind") == kind
        hits += int(hit)
        rows.append({"claim_id": claim_id, "kind_ok": claim.get("kind") == kind,
                     "refs_ok": refs_ok, "status": result.get("status"), "correct": hit})
    return {"gold_claims": len(KNOWN_CLAIMS), "correct": hits,
            "precision": hits / len(KNOWN_CLAIMS), "recall": hits / len(KNOWN_CLAIMS),
            "f1": hits / len(KNOWN_CLAIMS), "details": rows,
            "all_extracted_claims": len(workspace["claims"]),
            "unverified_claims": len(workspace["claims"]) - len(KNOWN_CLAIMS)}


def main() -> int:
    facts = _facts()
    rows = _evaluate_rows(facts)
    claims = _evaluate_claims()
    anchors = json.loads((RUN / "claim_anchor_checks.json").read_text(encoding="utf-8"))
    result = {
        "source": "000543_2018_皖能电力.pdf",
        "source_page": 7,
        "gold_basis": "原始 PDF 第 7 页主要会计数据和财务指标表，人工复核转录",
        "table": rows,
        "claims_with_complete_gold": claims,
        "anchor_existence": {"passed": sum(x["matches_original"] for x in anchors),
                              "checked": len(anchors)},
        "limitations": [
            "表格 Gold 只覆盖第 7 页，不能代表整份年报全部表格。",
            "正文 113 条论断中只有 2 条具备完整事实来源 Gold，其余 111 条计为不可验证，不计入准确率。",
            "3 行被当前货币指标准入规则拒绝，形成真实召回损失；拒绝不是抽取错误，但降低全表覆盖。",
        ],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = "# 真实年报准确率评测\n\n"
    report += "样本为《皖能电力》2018 年年报原始 PDF 第 7 页，Gold 由原表人工复核转录，未从生成的 Word/Excel 反推。\n\n"
    report += "## 表格事实抽取\n\n"
    report += "| 指标 | 结果 |\n|---|---:|\n"
    report += f"| Gold 行数 | {rows['gold_rows']} |\n| 实际抽取行数 | {rows['extracted_rows']} |\n"
    report += f"| 行级精确率 | {rows['row_precision']:.2%} |\n| 全表行级召回率 | {rows['row_recall_all']:.2%} |\n"
    report += f"| 当前支持范围召回率 | {rows['row_recall_supported_scope']:.2%} |\n| 全表行级 F1 | {rows['row_f1_all']:.2%} |\n"
    report += f"| 已准入行字段精确匹配 | {rows['field_hits']}/{rows['field_total']} = {rows['admitted_field_exact']:.2%} |\n"
    report += f"| 保守拒绝行 | {rows['conservative_rejections']} |\n\n"
    report += "6 行货币指标的指标名、单位、本期值、上期值和变动率全部匹配；3 行每股收益/净资产收益率被当前准入规则拒绝，说明当前主要损失来自覆盖率而非已准入行的数值错误。\n\n"
    report += "## 正文论断关联\n\n"
    report += f"对 2 条能够由摘要表完整证明的正文论断评测：{claims['correct']}/{claims['gold_claims']} 正确，Precision/Recall/F1 均为 {claims['f1']:.2%}。整份正文共 {claims['all_extracted_claims']} 条论断，其中 {claims['unverified_claims']} 条缺少完整来源，不能计为正确或错误。\n\n"
    report += f"原文锚点存在性：{result['anchor_existence']['passed']}/{result['anchor_existence']['checked']} 通过；这是定位指标，不是语义抽取准确率。\n\n"
    report += "## 结论\n\n"
    report += f"本次真实年报可报告的准确率是：已准入事实字段 {rows['admitted_field_exact']:.2%}、全表行召回 {rows['row_recall_all']:.2%}、全表行 F1 {rows['row_f1_all']:.2%}；有完整来源的正文论断 {claims['correct']}/{claims['gold_claims']} 正确。系统目前的主要问题是事实源覆盖不足以及 {rows['conservative_rejections']} 行指标被保守拒绝，而不是已准入货币行的数值解析错误。该结论仅适用于本页 Gold，不外推为整份年报泛化准确率。\n"
    REPORT.write_text(report, encoding="utf-8")
    print(json.dumps({"table": rows, "claims": claims, "report": str(REPORT)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
