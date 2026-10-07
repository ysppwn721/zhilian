"""Score the reviewed claims against the saved real annual-report run.

This is intentionally a strict, abstention-aware score.  The saved run has
claim refs but no ranked candidate list, so Top-2/MRR are reported unavailable;
we do not invent a ranking from the shuffled review order.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "output" / "final_real_annual_verified_20261006" / "000543_2018_皖能电力"
REVIEW = ROOT / "output" / "final_real_annual_verified_20261006" / "real_annual_top1_review"
XLSX = REVIEW / "real_annual_top1_review.xlsx"
OUT = REVIEW / "real_annual_top1_result.json"
REPORT = REVIEW / "真实年报Top1复核结果_20261006.md"


def refs(value) -> list[str]:
    if value is None:
        return []
    return [x for x in re.split(r"[,，;；\s]+", str(value).strip()) if x]


def load_review() -> list[dict]:
    sheet = openpyxl.load_workbook(XLSX, data_only=True)["复核表"]
    rows = []
    for values in sheet.iter_rows(min_row=2, values_only=True):
        if not values[0]:
            continue
        candidate_ids = [values[i] for i in range(5, 29, 2) if values[i]]
        gold = refs(values[29])
        status = str(values[30] or "").strip()
        if status != "confirmed" or not gold:
            raise ValueError(f"复核行未完成或未确认: {values[0]}")
        if any(x not in candidate_ids for x in gold):
            raise ValueError(f"Gold fact_id 不在候选集合: {values[0]} -> {gold}")
        rows.append({
            "claim_id": values[0], "kind": values[1], "original": values[2],
            "source_location": values[3], "candidate_fact_ids": candidate_ids,
            "gold_refs": gold, "review_status": status, "review_note": values[31] or "",
        })
    return rows


def score(rows: list[dict]) -> dict:
    workspace = json.loads((RUN / "baseline_workspace.json").read_text(encoding="utf-8"))
    claims = {x["id"]: x for x in workspace["claims"]}
    details = []
    for row in rows:
        predicted = list(claims[row["claim_id"]].get("refs", []))
        gold = row["gold_refs"]
        if row["kind"] == "quote":
            correct = bool(predicted) and predicted[0] == gold[0]
        else:
            correct = bool(predicted) and set(predicted) == set(gold)
        details.append({
            "claim_id": row["claim_id"], "kind": row["kind"], "original": row["original"],
            "gold_refs": gold, "predicted_refs": predicted,
            "answered": bool(predicted), "correct": correct,
        })
    total = len(details)
    answered = sum(x["answered"] for x in details)
    correct = sum(x["correct"] for x in details)
    quote = [x for x in details if x["kind"] == "quote"]
    growth = [x for x in details if x["kind"] == "growth"]

    def metrics(items):
        n = len(items)
        a = sum(x["answered"] for x in items)
        c = sum(x["correct"] for x in items)
        return {
            "total": n, "answered": a, "coverage": a / n if n else 0.0,
            "strict_top1_or_exact": c / n if n else 0.0,
            "answered_accuracy": c / a if a else 0.0,
            "correct": c,
        }

    return {
        "dataset": "real_annual_top1_review",
        "source": "000543_2018_皖能电力.pdf",
        "gold_claims": total,
        "answered": answered,
        "coverage": answered / total if total else 0.0,
        "strict_exact_match": correct / total if total else 0.0,
        "answered_accuracy": correct / answered if answered else 0.0,
        "quote": metrics(quote), "growth": metrics(growth),
        "top2": None, "mrr": None,
        "ranking_note": "当前真实年报运行只保存 refs，没有保存候选排名；不从复核表的随机候选顺序推导 Top-2/MRR。",
        "details": details,
    }


def main() -> int:
    rows = load_review()
    result = score(rows)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Keep the reviewed labels machine-readable without modifying the source run.
    (REVIEW / "real_annual_top1_review_filled.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8"
    )
    q, g = result["quote"], result["growth"]
    report = f"""# 真实年报 Top-1 复核结果

样本为《皖能电力》2018 年年报，人工确认 {result['gold_claims']} 条论断的正确事实来源。评分使用已保存的真实年报运行结果；没有 refs 的论断按严格口径计为未回答。

| 指标 | 结果 |
|---|---:|
| 人工 Gold 论断 | {result['gold_claims']} |
| 已回答 | {result['answered']} |
| 严格整体准确率（含拒答） | {result['strict_exact_match']:.2%} |
| 已回答样本准确率 | {result['answered_accuracy']:.2%} |
| 覆盖率 | {result['coverage']:.2%} |
| 单值 Top-1（严格） | {q['correct']}/{q['total']} = {q['strict_top1_or_exact']:.2%} |
| 单值已回答准确率 | {q['correct']}/{q['answered']} = {q['answered_accuracy']:.2%} |
| 增长双来源精确匹配（严格） | {g['correct']}/{g['total']} = {g['strict_top1_or_exact']:.2%} |
| 增长已回答准确率 | {g['correct']}/{g['answered']} = {g['answered_accuracy']:.2%} |
| Top-2 / MRR | 未计算 |

## 逐条结果

| claim_id | 类型 | 原文 | Gold | 本次运行 refs | 结果 |
|---|---|---|---|---|---|
"""
    for x in result["details"]:
        report += f"| `{x['claim_id']}` | {x['kind']} | {x['original']} | `{','.join(x['gold_refs'])}` | `{','.join(x['predicted_refs']) or '空'}` | {'正确' if x['correct'] else ('拒答' if not x['answered'] else '错误')} |\n"
    report += "\n当前结果说明：这 6 条 Gold 由人工确认，但真实运行的事实源只包含 12 条摘要事实，因来源不足而拒答的 4 条仍计入严格分母。该结果反映当前运行的覆盖与关联能力，不外推为整份年报或行业准确率。\n"
    report += "\n候选排序没有在原始运行中保存，因此不能从随机复核顺序推导 Top-2 或 MRR；后续若需完整排序指标，应在同一固定候选集上重新运行并保存每个候选的模型分数。\n"
    REPORT.write_text(report, encoding="utf-8")
    print(json.dumps({"result": str(OUT), "report": str(REPORT), "metrics": {"strict": result["strict_exact_match"], "coverage": result["coverage"], "quote": q, "growth": g}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
