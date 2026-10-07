"""Evaluate the annual-report reranker on the reviewed real-report Gold.

Unlike the original real-report run, this evaluator preserves every candidate
score and computes rank-based metrics from the fixed, human-confirmed set.
Growth claims are scored independently for the prior and current periods, then
checked as a two-source set.  The model never changes the source workspace.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "output" / "final_real_annual_verified_20261006" / "000543_2018_皖能电力"
REVIEW = ROOT / "output" / "final_real_annual_verified_20261006" / "real_annual_top1_review"
OUT = REVIEW / "real_annual_ranked_result.json"
REPORT = REVIEW / "真实年报Top1模型排序结果_20261006.md"

sys.path.insert(0, str(ROOT))
from zhilian import reranker  # noqa: E402


def load_review() -> list[dict]:
    rows = []
    for line in (REVIEW / "real_annual_top1_review_filled.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("review_status") != "confirmed" or not row.get("gold_refs"):
            raise ValueError(f"复核行未确认: {row.get('claim_id')}")
        rows.append(row)
    return rows


def load_facts() -> dict[str, dict]:
    workspace = json.loads((RUN / "baseline_workspace.json").read_text(encoding="utf-8"))
    return {fact["id"]: fact for fact in workspace["facts"]}


def score_rows(rows: list[dict], facts: dict[str, dict]) -> list[dict]:
    results = []
    with reranker.using_profile("annual"):
        status = reranker.status()
        if not status["enabled"]:
            raise RuntimeError(f"年报 reranker 未启用: {status}")
        for row in rows:
            candidates = [facts[fid] for fid in row["candidate_fact_ids"]]
            scores = reranker.score_pairs(row["original"], candidates, batch_size=16)
            by_id = {item["fact_id"]: item for item in scores}
            ordered = sorted(scores, key=lambda item: (-item["raw_score"], item["fact_id"]))
            top = ordered[0] if ordered else None
            selected = []
            if row["kind"] == "quote":
                if top:
                    selected = [top["fact_id"]]
            else:
                # A growth claim needs one source per period.  Selecting the
                # best current and prior fact separately prevents a strong
                # current-period score from hiding a missing prior source.
                for period in ("上期", "本期"):
                    period_scores = [item for item in scores if facts[item["fact_id"]].get("period") == period]
                    if period_scores:
                        selected.append(max(period_scores, key=lambda item: (item["raw_score"], item["fact_id"]))["fact_id"])
            gate = reranker.choose(ordered) if ordered else {"action": "abstain", "refs": [], "reason": "没有候选"}
            if row["kind"] == "growth":
                by_period = {}
                period_refs = []
                for period in ("上期", "本期"):
                    period_scores = [item for item in scores if facts[item["fact_id"]].get("period") == period]
                    period_gate = reranker.choose(period_scores) if period_scores else {
                        "action": "abstain", "refs": [], "reason": f"缺少{period}候选"
                    }
                    by_period[period] = period_gate
                    period_refs.extend(period_gate.get("refs", []))
                gate = {
                    "action": "link" if len(period_refs) == 2 else "abstain",
                    "refs": period_refs if len(period_refs) == 2 else [],
                    "reason": "增长题两期候选均通过门控" if len(period_refs) == 2 else "增长题未能为两期来源都通过门控",
                    "by_period": by_period,
                }
            results.append({
                "claim_id": row["claim_id"],
                "kind": row["kind"],
                "original": row["original"],
                "gold_refs": row["gold_refs"],
                "ranked_refs": selected,
                "predicted_refs": gate.get("refs", []),
                "answered": gate.get("action") == "link",
                "gate": gate,
                "top_candidate": top,
                "ranked_candidates": [
                    {**item, "period": facts[item["fact_id"]].get("period"), "metric": facts[item["fact_id"]].get("metric")}
                    for item in ordered
                ],
            })
    return status, results


def metrics(details: list[dict]) -> dict:
    def rank_exact(item: dict) -> bool:
        if item["kind"] == "quote":
            return bool(item["ranked_refs"]) and item["ranked_refs"][0] == item["gold_refs"][0]
        return set(item["ranked_refs"]) == set(item["gold_refs"])

    def gated_exact(item: dict) -> bool:
        if not item["answered"]:
            return False
        if item["kind"] == "quote":
            return item["predicted_refs"][:1] == item["gold_refs"][:1]
        return set(item["predicted_refs"]) == set(item["gold_refs"])

    def reciprocal_rank(item: dict) -> float:
        gold = set(item["gold_refs"])
        for index, candidate in enumerate(item["ranked_candidates"], start=1):
            if candidate["fact_id"] in gold:
                return 1.0 / index
        return 0.0

    quote = [item for item in details if item["kind"] == "quote"]
    growth = [item for item in details if item["kind"] == "growth"]
    all_items = details
    answered = sum(item["answered"] for item in all_items)
    correct = sum(gated_exact(item) for item in all_items)
    return {
        "gold_claims": len(all_items),
        "answered": answered,
        "strict_exact_match": correct / len(all_items),
        "coverage": answered / len(all_items),
        "answered_accuracy": correct / max(1, answered),
        "quote": {
            "total": len(quote),
            "top1_correct": sum(rank_exact(item) for item in quote),
            "top1_accuracy": sum(rank_exact(item) for item in quote) / len(quote),
            "top2_hit": sum(any(candidate["fact_id"] == item["gold_refs"][0] for candidate in item["ranked_candidates"][:2]) for item in quote),
            "top2_accuracy": sum(any(candidate["fact_id"] == item["gold_refs"][0] for candidate in item["ranked_candidates"][:2]) for item in quote) / len(quote),
        },
        "growth": {
            "total": len(growth),
            "source_set_exact": sum(rank_exact(item) for item in growth),
            "source_set_exact_match": sum(rank_exact(item) for item in growth) / len(growth),
            "gated_exact": sum(gated_exact(item) for item in growth),
            "answered_accuracy": sum(gated_exact(item) for item in growth) / max(1, sum(item["answered"] for item in growth)),
        },
        "mrr": sum(reciprocal_rank(item) for item in all_items) / len(all_items),
        "gate": {
            "abstained": sum(item["gate"].get("action") == "abstain" for item in all_items),
            "abstention_rate": sum(item["gate"].get("action") == "abstain" for item in all_items) / len(all_items),
        },
    }


def main() -> int:
    os.environ.setdefault("ZHILIAN_ANNUAL_RERANKER_ENABLED", "1")
    os.environ.setdefault("ZHILIAN_LOCAL_RERANKER_DEVICE", "cpu")
    rows = load_review()
    facts = load_facts()
    status, details = score_rows(rows, facts)
    result = {
        "dataset": "real_annual_top1_review",
        "source": "000543_2018_皖能电力.pdf",
        "model": status,
        "metrics": metrics(details),
        "details": details,
        "note": "固定人工 Gold 与固定候选集上的年报修正版 BERT 排序结果；不是整份年报或行业泛化准确率。",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = result["metrics"]
    lines = [
        "# 真实年报 Top-1 模型排序结果",
        "",
        "模型：年报修正版 BERT（本地 ONNX，CPU）。候选集合与人工复核表完全一致；每个候选的原始分数和排名均已保存。",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 人工 Gold 论断 | {m['gold_claims']} |",
        f"| 门控后严格整体准确率（含拒答） | {m['strict_exact_match']:.2%} |",
        f"| 门控后覆盖率 | {m['coverage']:.2%} |",
        f"| 门控后已回答准确率 | {m['answered_accuracy']:.2%} |",
        f"| 单值 Top-1 | {m['quote']['top1_correct']}/{m['quote']['total']} = {m['quote']['top1_accuracy']:.2%} |",
        f"| 单值 Top-2 | {m['quote']['top2_hit']}/{m['quote']['total']} = {m['quote']['top2_accuracy']:.2%} |",
        f"| 增长双来源排名精确匹配（不含门控） | {m['growth']['source_set_exact']}/{m['growth']['total']} = {m['growth']['source_set_exact_match']:.2%} |",
        f"| 门控后增长双来源准确 | {m['growth']['gated_exact']}/{m['growth']['total']} |",
        f"| MRR（首个 Gold 来源） | {m['mrr']:.4f} |",
        f"| 阈值拒答率 | {m['gate']['abstention_rate']:.2%} |",
        "",
        "说明：排名指标只衡量候选排序。门控后回答只统计通过置信阈值的 refs；增长题需要上期、本期候选分别通过门控，缺少任何一期都拒答。",
        "",
        "| claim_id | 类型 | Gold | 排序候选 | 排序命中 | 门控 refs | 门控结果 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in details:
        rank_ok = "命中" if ((item["kind"] == "quote" and item["ranked_refs"][:1] == item["gold_refs"][:1]) or (item["kind"] == "growth" and set(item["ranked_refs"]) == set(item["gold_refs"]))) else "未命中"
        gate_ok = "正确" if item["answered"] and ((item["kind"] == "quote" and item["predicted_refs"][:1] == item["gold_refs"][:1]) or (item["kind"] == "growth" and set(item["predicted_refs"]) == set(item["gold_refs"]))) else ("拒答" if not item["answered"] else "错误")
        lines.append(f"| `{item['claim_id']}` | {item['kind']} | `{','.join(item['gold_refs'])}` | `{','.join(item['ranked_refs'])}` | {rank_ok} | `{','.join(item['predicted_refs']) or '空'}` | {gate_ok} |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(OUT), "report": str(REPORT), "model": status, "metrics": m}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
