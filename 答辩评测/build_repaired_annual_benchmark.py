"""Build a corrected, programmatic annual-report ranking diagnostic set.

The existing frozen holdout is read only and remains unchanged.  This new
dataset uses real same-report facts as distractors, balances current/prior
single-source claims, and evaluates growth claims as two-source sets.
"""
from __future__ import annotations

import hashlib
import json
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from build_external_programmatic_holdout_all import REPORT_DIR, parse_report, report_year


ROOT = Path(__file__).resolve().parent
OLD_HOLDOUT = ROOT / "external_programmatic_holdout_all.jsonl"
OLD_STATS = ROOT / "external_programmatic_holdout_all_stats.json"
DEFAULT_OUT = ROOT / "annual_benchmark_repaired_20261004_v2"
SEED = 20261004

# Controlled aliases are a weakly labeled diagnostic stratum, not human gold.
ALIASES = {
    "归属于上市公司股东的净利润": "归母净利润",
    "经营活动产生的现金流量净额": "经营活动净现金流",
    "归属于上市公司股东的净资产": "归母净资产",
    "营业收入": "营收",
    "销售费用": "销售开支",
    "管理费用": "管理开支",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_value(raw: str) -> Decimal:
    value = raw.replace(",", "").replace("−", "-").strip()
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    if value.endswith("%"):
        value = value[:-1]
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a numeric value: {raw!r}") from exc


def growth_percent(current: str, prior: str, parsed: str | None) -> str:
    if parsed:
        return parsed.removesuffix("%")
    old = decimal_value(prior)
    if old == 0:
        return "未披露"
    value = (decimal_value(current) / old - Decimal(1)) * Decimal(100)
    return f"{value.quantize(Decimal('0.01')):+f}%"


def make_fact(group: str, index: int, code: str, year: int, item: dict, period: str) -> dict:
    year_value = year if period == "current" else year - 1
    fact_id = f"{group}-m{index}-{period}"
    fact = {
        "fact_id": fact_id,
        "subject": f"上市公司{code}",
        "metric": item["metric"],
        "period": f"{year_value}年",
        "unit": item["unit"],
        "scope": "未核验",
        "value": item[period],
        "source_page": item["page"],
    }
    fact["fact_text"] = "；".join(
        f"{key}={fact[key]}" for key in ("subject", "metric", "period", "unit", "scope")
    )
    return fact


def build_claim_rows(
    *, group: str, code: str, year: int, metric_index: int, item: dict,
    facts: list[dict], variant: str, metric_text: str, seed: int,
) -> list[dict]:
    metric = item["metric"]
    changed = metric_text != metric
    variant_name = "controlled_alias" if changed else "literal"
    queries = [
        ("quote_current", f"上市公司{code}{year}年{metric_text}为{item['current']}{item['unit']}", "current", 1),
        ("quote_prior", f"上市公司{code}{year - 1}年{metric_text}为{item['prior']}{item['unit']}", "prior", 1),
        ("growth_set", f"上市公司{code}{year}年{metric_text}同比变化{growth_percent(item['current'], item['prior'], item.get('change'))}", "both", 2),
    ]
    rows: list[dict] = []
    for task_type, claim_text, target, expected_k in queries:
        claim_id = f"{group}-m{metric_index}-{variant_name}-{task_type}"
        if task_type == "growth_set":
            selected = facts
        else:
            target_period = f"{year if target == 'current' else year - 1}年"
            selected = [
                fact for fact in facts
                if (fact["metric"] == metric and fact["period"] == target_period)
                or (fact["metric"] != metric and fact["period"] == target_period)
                or (fact["metric"] == metric and fact["period"] != target_period)
            ]
        gold_ids = {
            fact["fact_id"] for fact in selected
            if fact["metric"] == metric and (
                task_type == "growth_set"
                or fact["period"] == f"{year if target == 'current' else year - 1}年"
            )
        }
        if len(gold_ids) != expected_k:
            continue
        gold_periods = {fact["period"] for fact in selected if fact["fact_id"] in gold_ids}
        ordered = sorted(
            selected,
            key=lambda fact: hashlib.sha256(
                f"{seed}:{claim_id}:{fact['fact_id']}".encode("utf-8")
            ).hexdigest(),
        )
        for position, fact in enumerate(ordered):
            if fact["fact_id"] in gold_ids:
                candidate_role = "gold_source"
            elif fact["metric"] != metric and fact["period"] in gold_periods:
                candidate_role = "same_subject_same_period_other_metric"
            elif fact["metric"] == metric:
                candidate_role = "same_metric_wrong_period"
            else:
                candidate_role = "other_same_report_fact"
            rows.append({
                "claim_id": claim_id,
                "claim_text": claim_text,
                "fact_id": fact["fact_id"],
                "fact_text": fact["fact_text"],
                "group_id": group,
                "company": code,
                "label": int(fact["fact_id"] in gold_ids),
                "gold_fact_ids": sorted(gold_ids),
                "gold_metric": metric,
                "expected_k": expected_k,
                "task_type": task_type,
                "target_period": (
                    f"{year}年" if target == "current" else
                    (f"{year - 1}年" if target == "prior" else "current+prior")
                ),
                "claim_variant": variant_name,
                "alias": metric_text if changed else None,
                "candidate_role": candidate_role,
                "candidate_position": position,
                "source_file": variant,
                "source_page": item["page"],
                "period": fact["period"],
                "metric": fact["metric"],
                "value": fact["value"],
                "unit": fact["unit"],
                "programmatic": True,
            })
    return rows


def audit_rows(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["claim_id"], []).append(row)

    role_counts: Counter[str] = Counter()
    position_positive_counts: Counter[int] = Counter()
    candidate_counts: Counter[int] = Counter()
    for claim_id, group in grouped.items():
        first = group[0]
        expected_k = int(first["expected_k"])
        gold_ids = set(first["gold_fact_ids"])
        ids = [row["fact_id"] for row in group]
        positions = [row["candidate_position"] for row in group]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate candidate fact in {claim_id}")
        if sorted(positions) != list(range(len(group))):
            raise ValueError(f"candidate positions are not a permutation in {claim_id}")
        if sum(int(row["label"]) for row in group) != expected_k:
            raise ValueError(f"unexpected positive count in {claim_id}")
        if {row["fact_id"] for row in group if row["label"]} != gold_ids:
            raise ValueError(f"labels do not match gold_fact_ids in {claim_id}")
        gold_periods = {row["period"] for row in group if row["fact_id"] in gold_ids}
        for row in group:
            if row["company"] != first["company"] or row["group_id"] != first["group_id"]:
                raise ValueError(f"candidate subject/report mismatch in {claim_id}")
            if row["fact_id"] in gold_ids:
                expected_role = "gold_source"
            elif row["metric"] != first["gold_metric"] and row["period"] in gold_periods:
                expected_role = "same_subject_same_period_other_metric"
            elif row["metric"] == first["gold_metric"]:
                expected_role = "same_metric_wrong_period"
            else:
                expected_role = "other_same_report_fact"
            if row["candidate_role"] != expected_role:
                raise ValueError(
                    f"candidate role mismatch in {claim_id}: {row['fact_id']} "
                    f"got {row['candidate_role']}, expected {expected_role}"
                )
            role_counts[row["candidate_role"]] += 1
            if row["candidate_position"] == 0 and row["label"]:
                position_positive_counts[0] += 1
        if not any(
            row["candidate_role"] == "same_subject_same_period_other_metric"
            for row in group
        ):
            raise ValueError(f"no same-subject, same-period, other-metric distractor in {claim_id}")
        candidate_counts[len(group)] += 1

    quote_groups = [group for group in grouped.values() if group[0]["task_type"] != "growth_set"]
    position_zero_positive_rate = position_positive_counts[0] / max(1, len(grouped))
    return {
        "candidate_role_counts": dict(role_counts),
        "candidate_count_distribution": {str(key): value for key, value in sorted(candidate_counts.items())},
        "all_claims_have_same_period_other_metric_distractor": True,
        "all_labels_match_gold_fact_ids": True,
        "all_candidate_orders_are_permutations": True,
        "quote_claims": len(quote_groups),
        "quote_position_0_positive_rate": round(
            sum(
                next(row["label"] for row in group if row["candidate_position"] == 0)
                for group in quote_groups
            ) / max(1, len(quote_groups)), 6
        ),
        "all_claim_position_0_positive_rate": round(position_zero_positive_rate, 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    out_dir = args.out
    if out_dir.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {out_dir}")
    old_hash = sha256(OLD_HOLDOUT)
    old_stats = json.loads(OLD_STATS.read_text(encoding="utf-8"))
    approved_files = [
        row["file"] for row in old_stats["reports_detail"]
        if "error" not in row and row.get("claims", 0) > 0
    ]
    all_rows: list[dict] = []
    report_stats = []
    for filename in approved_files:
        path = REPORT_DIR / filename
        if not path.is_file():
            report_stats.append({"file": filename, "status": "missing"})
            continue
        code = path.stem[:6]
        year = report_year(path)
        group = f"repaired-pdf-{code}-{year}-{hashlib.sha1(path.name.encode('utf-8')).hexdigest()[:6]}"
        parsed = parse_report(path)[:8]
        parsed = [item for item in parsed if item.get("current") and item.get("prior")]
        facts = [
            make_fact(group, index, code, year, item, period)
            for index, item in enumerate(parsed, 1)
            for period in ("current", "prior")
        ]
        aliases_used = 0
        report_rows = []
        for index, item in enumerate(parsed, 1):
            report_rows.extend(build_claim_rows(
                group=group, code=code, year=year, metric_index=index, item=item,
                facts=facts, variant=filename, metric_text=item["metric"], seed=args.seed,
            ))
            alias = ALIASES.get(item["metric"])
            if alias:
                aliases_used += 1
                report_rows.extend(build_claim_rows(
                    group=group, code=code, year=year, metric_index=index, item=item,
                    facts=facts, variant=filename, metric_text=alias, seed=args.seed,
                ))
        all_rows.extend(report_rows)
        report_stats.append({
            "file": filename,
            "code": code,
            "year": year,
            "metrics": len(parsed),
            "claims": len({row["claim_id"] for row in report_rows}),
            "controlled_alias_metrics": aliases_used,
            "sha256": sha256(path),
        })

    if not all_rows:
        raise SystemExit("No benchmark rows were generated")
    audit = audit_rows(all_rows)
    out_dir.mkdir(parents=True)
    dataset = out_dir / "benchmark.jsonl"
    with dataset.open("w", encoding="utf-8") as stream:
        for row in all_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    grouped: dict[str, list[dict]] = {}
    for row in all_rows:
        grouped.setdefault(row["claim_id"], []).append(row)
    summary = {
        "dataset": str(dataset),
        "rows": len(all_rows),
        "claims": len(grouped),
        "reports": len({row["source_file"] for row in all_rows}),
        "companies": len({row["company"] for row in all_rows}),
        "task_counts": dict(Counter(rows[0]["task_type"] for rows in grouped.values())),
        "variant_counts": dict(Counter(rows[0]["claim_variant"] for rows in grouped.values())),
        "period_positive_claims": {
            key: sum(rows[0]["task_type"] == key for rows in grouped.values())
            for key in ("quote_current", "quote_prior")
        },
        "source_set_growth_claims": sum(rows[0]["task_type"] == "growth_set" for rows in grouped.values()),
        "position_0_positive_rate": round(sum(
            next(row["label"] for row in rows if row["candidate_position"] == 0)
            for rows in grouped.values()
        ) / len(grouped), 6),
        "audit": audit,
        "frozen_source_sha256_before": old_hash,
        "frozen_source_sha256_after": sha256(OLD_HOLDOUT),
        "dataset_sha256": sha256(dataset),
        "reports_detail": report_stats,
        "programmatic": True,
        "label_limitations": [
            "Labels are generated from parsed PDF text and known field construction, not human annotation.",
            "The PDF parser infers some units; reporting scope remains unverified.",
            "Controlled aliases are weakly labeled diagnostics and need manual validation before gold claims.",
            "This is a corrected diagnostic derivative of the existing held-out reports, not a new external corpus.",
        ],
        "construction": {
            "same_subject_same_period_other_metric_distractors": True,
            "single_source_current_and_prior_balanced": True,
            "candidate_order_seed": args.seed,
            "growth_metric": "top-2 source-set exact match and completeness",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "README.md").write_text(
        "# 修正版年报候选排序诊断集\n\n"
        "此目录是由冻结年报派生的程序化诊断集。原冻结 JSONL 未修改。" 
        "它补入同主体、同期间、不同指标的真实候选；单来源引用题的本期与上期配平；" 
        "增长题按本期和上期两个来源做集合匹配。\n\n"
        "这不是人工标注 gold，也不是新外部语料。原 PDF 的字段解析、单位推断、合并口径假设和受控同义改写都可能带来标签噪声。\n",
        encoding="utf-8",
    )
    if sha256(OLD_HOLDOUT) != old_hash:
        raise SystemExit("Frozen holdout changed unexpectedly")
    print(json.dumps({key: summary[key] for key in (
        "rows", "claims", "reports", "companies", "task_counts", "variant_counts",
        "period_positive_claims", "source_set_growth_claims", "position_0_positive_rate", "audit",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
