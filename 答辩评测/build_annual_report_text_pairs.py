"""Build review candidates from independently sourced annual-report pairs.

Text anchors do not prove the candidate's metric, period or accounting scope.
The output is not trainable until a separate label audit approves it.  Frozen
evaluation companies are excluded before any output is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

NUM = re.compile(r"[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?")
SPLIT_ORDER = ("train", "dev", "test")
SEMANTIC_ALIASES = {
    "营业收入": ("营收", "销售收入"),
    "营业成本": ("经营成本",),
    "净利润": ("纯利润",),
    "销售费用": ("销售支出",),
    "管理费用": ("管理支出",),
    "研发费用": ("研发支出",),
    "资产总计": ("总资产",),
    "负债合计": ("总负债",),
    "应收账款": ("应收款项",),
}
GROWTH_TOKENS = ("同比", "环比", "增长", "减少", "下降", "上升", "增幅", "增减", "变动")


def clean(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def norm_num(value: str) -> str:
    return clean(value).replace(",", "").replace("−", "-").replace("(", "-").replace(")", "")


def metric_from_fact(fact_text: str) -> str | None:
    match = re.search(r"(?:^|；)metric=([^；]+)", fact_text)
    return match.group(1) if match else None


def claim_is_growth(claim_text: str) -> bool:
    return any(token in claim_text for token in ("较上年", "较上期", "同比", "环比", "变化", "增长", "减少", "下降"))


def report_sentences(path: Path) -> list[tuple[int, str]]:
    import pymupdf

    doc = pymupdf.open(path)
    result: list[tuple[int, str]] = []
    for page_no, page in enumerate(doc, 1):
        text = page.get_text("text") or ""
        for sentence in re.split(r"[。！？；;\n]", text):
            sentence = re.sub(r"\s+", " ", sentence).strip()
            if 8 <= len(sentence) <= 240 and any("\u4e00" <= c <= "\u9fff" for c in sentence):
                result.append((page_no, sentence))
    doc.close()
    return result


def find_anchor(row: dict, sentences: list[tuple[int, str]]) -> tuple[int, str] | None:
    metric = metric_from_fact(row.get("fact_text", ""))
    if not metric:
        return None
    claim = row.get("claim_text", "")
    target_numbers = [norm_num(m.group(0)) for m in NUM.finditer(claim)]
    if claim_is_growth(claim):
        candidates = [item for item in sentences if metric in item[1] and (
            ("%" in item[1] or "百分点" in item[1])
            and any(token in item[1] for token in GROWTH_TOKENS)
            and "占" not in item[1]
        )]
    else:
        # A quote claim without a rendered number is a heading or a table
        # label, not an auditable numeric assertion.  Do not turn it into a
        # positive training pair merely because it mentions the metric.
        if not target_numbers:
            return None
        candidates = [item for item in sentences if metric in item[1]]
        salient = [num for num in target_numbers if len(num.lstrip("-")) >= 3 and not (len(num) == 4 and num.startswith("20"))]
        if salient:
            exact = []
            for item in candidates:
                rendered = {norm_num(match.group(0)) for match in NUM.finditer(item[1])}
                if any(num in rendered for num in salient):
                    exact.append(item)
            # Do not silently use an unrelated heading or table label when
            # the numeric anchor is absent from the candidate sentence.
            if not exact:
                return None
            candidates = exact
    return candidates[0] if candidates else None


def company_key(group: str) -> str:
    match = re.search(r"(?:^|[-_])(\d{6})(?:[-_]|$)", group)
    if not match:
        raise ValueError(f"Company code is required for annual-report group: {group}")
    return match.group(1)


def audit_training_sources(rows: list[dict], frozen_rows: list[dict]) -> None:
    companies = {company_key(row['group_id']) for row in rows}
    frozen_companies = {company_key(row['group_id']) for row in frozen_rows}
    overlap = companies & frozen_companies
    if overlap:
        raise ValueError(f"Frozen evaluation companies cannot be used for training: {sorted(overlap)}")


def split_groups(groups: list[str]) -> dict[str, str]:
    companies = {company_key(group) for group in groups}
    if len(companies) < 3:
        raise ValueError("At least three independent companies are required for train/dev/test")
    ordered = sorted(companies, key=lambda company: hashlib.sha256(company.encode()).hexdigest())
    n = len(ordered)
    train_end = min(n - 2, max(1, round(n * 0.70)))
    dev_end = min(n - 1, train_end + max(1, round(n * 0.15)))
    result = {}
    for index, company in enumerate(ordered):
        result[company] = "train" if index < train_end else ("dev" if index < dev_end else "test")
    return {group: result[company_key(group)] for group in groups}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True,
                        help="Independent weak-label pairs, never the frozen evaluation dataset")
    parser.add_argument("--frozen-reference", type=Path,
                        default=Path(__file__).parent / "external_programmatic_holdout_all.jsonl")
    parser.add_argument("--reports", type=Path, default=Path(__file__).parent / "cn_reports")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "annual_report_text_pairs_v1")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.source.open(encoding="utf-8") if line.strip()]
    frozen_rows = [json.loads(line) for line in args.frozen_reference.open(encoding="utf-8") if line.strip()]
    audit_training_sources(rows, frozen_rows)
    by_group: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_group[row["group_id"]].append(row)
    group_splits = split_groups(list(by_group))
    report_cache: dict[str, list[tuple[int, str]]] = {}
    output: list[dict] = []
    semantic_variants = 0
    stats: list[dict] = []
    for group, group_rows in sorted(by_group.items()):
        file_name = group_rows[0].get("source_file")
        pdf = args.reports / file_name if file_name else None
        if not pdf or not pdf.exists():
            stats.append({"group_id": group, "file": file_name, "kept": 0, "reason": "source_pdf_missing"})
            continue
        sentences = report_cache.setdefault(file_name, report_sentences(pdf))
        kept_claims: set[str] = set()
        anchors: dict[str, tuple[int, str]] = {}
        # Anchor one positive candidate per assertion.  Negative candidates
        # must not overwrite the assertion's original text or source page.
        positive_by_claim = {}
        for row in group_rows:
            if row.get('label') == 1:
                positive_by_claim.setdefault(row['claim_id'], row)
        for row in positive_by_claim.values():
            anchor = find_anchor(row, sentences)
            if anchor:
                kept_claims.add(row["claim_id"])
                anchors[row["claim_id"]] = anchor
        split = group_splits[group]
        for row in group_rows:
            anchor = anchors.get(row["claim_id"])
            if not anchor:
                continue
            item = dict(row)
            item["claim_text"] = anchor[1]
            item["source_page"] = anchor[0]
            item["split"] = split
            item["real_pdf_text"] = True
            output.append(item)
            if split == "train":
                metric = metric_from_fact(item.get("fact_text", ""))
                for index, alias in enumerate(SEMANTIC_ALIASES.get(metric or "", ()), 1):
                    if metric and metric in item["claim_text"]:
                        variant = dict(item)
                        variant["claim_id"] = f"{item['claim_id']}-sem{index}"
                        variant["claim_text"] = item["claim_text"].replace(metric, alias)
                        variant["semantic_variant"] = True
                        variant["real_pdf_text"] = False
                        variant["original_pdf_text"] = item["claim_text"]
                        output.append(variant)
                        semantic_variants += 1
        stats.append({"group_id": group, "file": file_name, "split": split,
                      "source_claims": len({r["claim_id"] for r in group_rows}),
                      "kept_claims": len(kept_claims),
                      "rows": sum(1 for r in output if r["group_id"] == group)})

    args.out.mkdir(parents=True, exist_ok=True)
    split_counts = {}
    for split in SPLIT_ORDER:
        split_rows = [row for row in output if row["split"] == split]
        split_counts[split] = {"rows": len(split_rows), "claims": len({r["claim_id"] for r in split_rows}),
                               "groups": len({r["group_id"] for r in split_rows})}
        with (args.out / f"{split}.jsonl").open("w", encoding="utf-8") as stream:
            for row in split_rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "source": str(args.source),
        "reports": str(args.reports),
        "ready_for_training": False,
        "validation_status": "pending_label_scope_period_and_candidate_audit",
        "label_basis": "unverified programmatic candidate labels from independent annual reports",
        "claim_basis": "PDF text anchors plus explicitly marked train-only rewrites; no template fallback",
        "frozen_reference": str(args.frozen_reference),
        "split_basis": "company-disjoint, including all years and corrections of a company",
        "group_split": split_counts,
        "semantic_variants_train_only": semantic_variants,
        "stats": stats,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(split_counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
