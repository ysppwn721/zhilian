"""Rebuild a weak-label reranker set with balanced periods and hard negatives.

Only the existing train/dev/test source corpus is read.  The repaired annual
benchmark is never used as training input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "annual_reports_weak_training_20261003_v2"
DEFAULT_OUT = ROOT / "annual_reports_weak_training_repaired_20261004"
PERIOD_RE = re.compile(r"(?:^|；)period=([^；]+)")
METRIC_RE = re.compile(r"(?:^|；)metric=([^；]+)")


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fact_field(row: dict, field: str, pattern: re.Pattern[str]) -> str:
    if row.get(field) is not None:
        return str(row[field])
    match = pattern.search(str(row.get("fact_text", "")))
    return match.group(1).strip() if match else ""


def signature(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("fact_id", "")),
        fact_field(row, "metric", METRIC_RE),
        fact_field(row, "period", PERIOD_RE),
        str(row.get("unit", "")),
        str(row.get("fact_value", "")),
    )


def stable_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query_text(metric: str, period: str, key: str) -> str:
    current = ["本报告期", "报告期内", "本期披露的"]
    prior = ["上年同期", "上一报告期", "上期披露的"]
    choices = current if period == "本期" else prior
    phrase = choices[int(stable_key(key)[:8], 16) % len(choices)]
    return f"请核对公司{phrase}{metric}对应的事实来源。"


def build_candidate(
    *, claim_id: str, claim_text: str, source_claim: str, group_id: str,
    company: str, split: str, fact: dict, label: int, gold_ids: list[str],
    gold_metric: str, expected_k: int, task_type: str, target_period: str,
    source_corpus: str,
) -> dict:
    metric = fact_field(fact, "metric", METRIC_RE)
    period = fact_field(fact, "period", PERIOD_RE)
    role = "gold_source" if label else (
        "same_metric_wrong_period" if metric == gold_metric
        else "same_subject_same_period_other_metric"
    )
    return {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "source_claim_id": source_claim,
        "fact_id": fact["fact_id"],
        "fact_text": fact["fact_text"],
        "group_id": group_id,
        "company": company,
        "split": split,
        "label": label,
        "label_source": "programmatic_weak_label_reconstructed",
        "task_type": task_type,
        "target_period": target_period,
        "expected_k": expected_k,
        "gold_fact_ids": sorted(gold_ids),
        "candidate_role": role,
        "metric": metric,
        "period": period,
        "unit": fact.get("unit", ""),
        "fact_value": fact.get("fact_value"),
        "source_corpus": source_corpus,
        "programmatic": True,
    }


def build_split(rows: list[dict], split: str) -> tuple[list[dict], dict]:
    by_claim: dict[str, list[dict]] = defaultdict(list)
    facts_by_group: dict[str, dict[str, dict]] = defaultdict(dict)
    fact_signatures: dict[str, dict[str, set[tuple]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        by_claim[row["claim_id"]].append(row)
        group = row["group_id"]
        fact_id = str(row.get("fact_id", ""))
        fact_signatures[group][fact_id].add(signature(row))
        current = facts_by_group[group].get(fact_id)
        if current is None or (row.get("label") == 1 and current.get("label") != 1):
            facts_by_group[group][fact_id] = dict(row)

    invalid_groups = {
        group for group, facts in fact_signatures.items()
        if any(len(variants) > 1 for variants in facts.values())
    }
    output: list[dict] = []
    quote_by_period: dict[str, list[list[dict]]] = {"本期": [], "上期": []}
    growth_claim_count = 0
    rejected_claims = Counter()
    positive_examples = 0

    for source_claim, group_rows in sorted(by_claim.items()):
        first = group_rows[0]
        group_id = first["group_id"]
        if group_id in invalid_groups:
            rejected_claims["fact_id_conflict_within_report_group"] += 1
            continue
        positives = [row for row in group_rows if int(row.get("label", 0)) == 1]
        positive_keys = {(str(row["fact_id"]), signature(row)[1], signature(row)[2]) for row in positives}
        positive_metrics = {metric for _, metric, _ in positive_keys}
        if not positives or len(positive_metrics) != 1:
            rejected_claims["missing_or_multiple_positive_metrics"] += 1
            continue
        gold_metric = next(iter(positive_metrics))
        if not gold_metric:
            rejected_claims["missing_metric"] += 1
            continue
        gold_by_period: dict[str, set[str]] = defaultdict(set)
        for fact_id, metric, period in positive_keys:
            if metric == gold_metric and period in {"本期", "上期"}:
                gold_by_period[period].add(fact_id)
        if not gold_by_period:
            rejected_claims["no_current_or_prior_positive"] += 1
            continue

        pool = list(facts_by_group[group_id].values())
        source_corpus = str(first.get("source_corpus", ""))
        company = str(first.get("company", group_id))

        for period, gold_ids in gold_by_period.items():
            gold_facts = [
                fact for fact in pool
                if fact["fact_id"] in gold_ids
                and fact_field(fact, "metric", METRIC_RE) == gold_metric
                and fact_field(fact, "period", PERIOD_RE) == period
            ]
            if len(gold_facts) != len(gold_ids):
                rejected_claims["positive_fact_missing_from_pool"] += 1
                continue
            candidate_facts = []
            for fact in pool:
                metric = fact_field(fact, "metric", METRIC_RE)
                fact_period = fact_field(fact, "period", PERIOD_RE)
                if not metric or fact_period not in {"本期", "上期"}:
                    continue
                if metric == gold_metric and fact_period == period and fact["fact_id"] not in gold_ids:
                    continue
                if (fact_period == period and metric != gold_metric) or (
                    metric == gold_metric and fact_period != period
                ):
                    candidate_facts.append(fact)
            candidate_facts.extend(gold_facts)
            unique = {str(fact["fact_id"]): fact for fact in candidate_facts}
            if len(unique) < 2 or not any(
                fact_field(fact, "metric", METRIC_RE) != gold_metric
                and fact_field(fact, "period", PERIOD_RE) == period
                for fact in unique.values()
            ):
                rejected_claims["insufficient_same_period_hard_negatives"] += 1
                continue
            claim_id = f"{source_claim}::repaired_quote_{period}"
            text = query_text(gold_metric, period, claim_id)
            gold_sorted = sorted(gold_ids)
            claim_rows = [
                build_candidate(
                    claim_id=claim_id,
                    claim_text=text,
                    source_claim=source_claim,
                    group_id=group_id,
                    company=company,
                    split=split,
                    fact=fact,
                    label=int(str(fact["fact_id"]) in gold_ids),
                    gold_ids=gold_sorted,
                    gold_metric=gold_metric,
                    expected_k=len(gold_ids),
                    task_type=f"quote_{period}",
                    target_period=period,
                    source_corpus=source_corpus,
                )
                for fact in unique.values()
            ]
            quote_by_period[period].append(claim_rows)

        if {"本期", "上期"}.issubset(gold_by_period):
            gold_ids = gold_by_period["本期"] | gold_by_period["上期"]
            if len(gold_ids) != 2:
                rejected_claims["ambiguous_growth_source_set"] += 1
                continue
            candidate_facts = []
            for fact in pool:
                metric = fact_field(fact, "metric", METRIC_RE)
                period = fact_field(fact, "period", PERIOD_RE)
                if period not in {"本期", "上期"}:
                    continue
                if metric == gold_metric and str(fact["fact_id"]) not in gold_ids:
                    continue
                candidate_facts.append(fact)
            unique = {str(fact["fact_id"]): fact for fact in candidate_facts}
            gold_facts = [fact for fact in pool if str(fact["fact_id"]) in gold_ids]
            unique.update({str(fact["fact_id"]): fact for fact in gold_facts})
            if len(unique) <= len(gold_ids) or not any(
                fact_field(fact, "metric", METRIC_RE) != gold_metric
                and fact_field(fact, "period", PERIOD_RE) in {"本期", "上期"}
                for fact in unique.values()
            ):
                rejected_claims["insufficient_growth_hard_negatives"] += 1
                continue
            claim_id = f"{source_claim}::repaired_growth"
            text = str(first["claim_text"])
            claim_rows = [
                build_candidate(
                    claim_id=claim_id,
                    claim_text=text,
                    source_claim=source_claim,
                    group_id=group_id,
                    company=company,
                    split=split,
                    fact=fact,
                    label=int(str(fact["fact_id"]) in gold_ids),
                    gold_ids=sorted(gold_ids),
                    gold_metric=gold_metric,
                    expected_k=2,
                    task_type="growth_set",
                    target_period="本期+上期",
                    source_corpus=source_corpus,
                )
                for fact in unique.values()
            ]
            output.extend(claim_rows)
            growth_claim_count += 1

    quote_counts = {period: len(claims) for period, claims in quote_by_period.items()}
    balanced_count = min(quote_counts.values())
    for period, claims in quote_by_period.items():
        selected = sorted(
            claims,
            key=lambda claim: stable_key(f"{split}:{period}:{claim[0]['claim_id']}"),
        )[:balanced_count]
        output.extend(row for claim in selected for row in claim)

    stats = {
        "source_rows": len(rows),
        "source_claims": len(by_claim),
        "quote_claims_before_balance": quote_counts,
        "balanced_quote_claims_per_period": balanced_count,
        "growth_claims": growth_claim_count,
        "rejected_claims": dict(rejected_claims),
        "output_rows": len(output),
        "output_claims": len({row["claim_id"] for row in output}),
        "output_companies": len({row["company"] for row in output}),
        "positive_rows": sum(int(row["label"]) == 1 for row in output),
        "negative_rows": sum(int(row["label"]) == 0 for row in output),
    }
    return output, stats


def audit_output(rows_by_split: dict[str, list[dict]]) -> dict:
    group_splits: dict[str, set[str]] = defaultdict(set)
    task_counts = Counter()
    role_counts = Counter()
    for split, rows in rows_by_split.items():
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[row["claim_id"]].append(row)
            group_splits[row["group_id"]].add(split)
        for claim_id, group in grouped.items():
            expected_k = group[0]["expected_k"]
            gold_ids = set(group[0]["gold_fact_ids"])
            gold_metric = next((row["metric"] for row in group if row["label"]), "")
            if sum(int(row["label"]) for row in group) != expected_k:
                raise ValueError(f"wrong positive count for {claim_id}")
            if {row["fact_id"] for row in group if row["label"]} != gold_ids:
                raise ValueError(f"gold source set mismatch for {claim_id}")
            if not any(
                not row["label"]
                and row["metric"] != gold_metric
                and row["period"] in {"本期", "上期"}
                for row in group
            ):
                raise ValueError(f"no same-period, other-metric negative for {claim_id}")
            task_counts[(split, group[0]["task_type"])] += 1
            for row in group:
                role_counts[row["candidate_role"]] += 1
    leakage = {group: sorted(splits) for group, splits in group_splits.items() if len(splits) > 1}
    if leakage:
        raise ValueError(f"group leakage: {list(leakage.items())[:5]}")
    return {
        "company_group_disjoint": True,
        "all_claims_have_same_period_other_metric_negatives": True,
        "all_labels_match_gold_fact_ids": True,
        "task_claim_counts": {f"{split}:{task}": count for (split, task), count in sorted(task_counts.items())},
        "candidate_role_counts": dict(role_counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {args.out}")

    manifest = json.loads((args.source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("company_disjoint") is not True or manifest.get("frozen_evaluation_reused") is not False:
        raise SystemExit("Source dataset does not prove company-disjoint splits or frozen-set isolation")
    rows_by_split = {}
    split_stats = {}
    for split in ("train", "dev", "test"):
        source_rows = read_rows(args.source / f"{split}.jsonl")
        output, stats = build_split(source_rows, split)
        rows_by_split[split] = output
        split_stats[split] = stats
    audit = audit_output(rows_by_split)

    args.out.mkdir(parents=True)
    for split, rows in rows_by_split.items():
        path = args.out / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "ready_for_training": all(bool(rows_by_split[split]) for split in rows_by_split),
        "label_quality": "programmatic_weak_labels_reconstructed",
        "evaluation_warning": "Not human gold; use as a research candidate only. Keep benchmark and frozen evaluation data separate.",
        "source": str(args.source),
        "source_training_rows": manifest["source_rows"],
        "frozen_evaluation_reused": False,
        "company_disjoint": True,
        "splits": split_stats,
        "audit": audit,
        "blocking_reasons": [
            "labels remain parser-derived weak labels and scope/unit assumptions are not human verified",
            "controlled query templates are synthetic and do not represent broad natural language",
        ],
    }
    (args.out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "README.md").write_text(
        "# 修正版年报弱监督训练集\n\n"
        "从既有训练语料的公司隔离 train/dev/test 划分重建。单值本期/上期查询配平；"
        "候选补入同主体、同期间、不同指标事实；增长题按本期和上期两个来源组成集合。\n\n"
        "所有标签仍是程序化弱标签，查询模板也由程序构造，不是人工标注 gold。此数据只用于训练研究候选，"
        "不得用于声称独立泛化准确率；诊断基准和冻结评测集未作为训练数据。\n",
        encoding="utf-8",
    )
    print(json.dumps({"splits": split_stats, "audit": audit}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
