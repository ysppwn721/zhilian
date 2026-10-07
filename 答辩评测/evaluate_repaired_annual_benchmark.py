"""Compare rerankers on the repaired annual-report diagnostic benchmark."""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODELS = {
    "bge": ROOT / "models" / "bge-reranker-v2-m3-onnx-int8",
    "bert_v1": ROOT / "models" / "zh_reranker_bert_ft",
    "bert_v2": ROOT / "models" / "zh_reranker_bert_ft_v2",
    "annual_weak_v1": ROOT / "models" / "zh_reranker_bert_annual_weak_v1",
    "annual_repaired_v1": ROOT / "models" / "zh_reranker_bert_annual_repaired_v1",
}
CONTROLLED_ALIASES = {
    "归属于上市公司股东的净利润": "归母净利润",
    "经营活动产生的现金流量净额": "经营活动净现金流",
    "归属于上市公司股东的净资产": "归母净资产",
    "营业收入": "营收",
    "销售费用": "销售开支",
    "管理费用": "管理开支",
}


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["claim_id"]].append(row)
    return grouped


def score_rows(rows: list[dict], model_name: str, batch_size: int, device: str) -> list[float]:
    if model_name == "bge":
        if device != "cpu":
            raise ValueError("The packaged BGE model has only CPUExecutionProvider in this environment")
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = MODELS[model_name]
        onnx_path = model_dir / "onnx" / "model_int8.onnx"
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        tokenizer.no_padding()
        pad_id = tokenizer.token_to_id("<pad>")
        if pad_id is None:
            raise ValueError("BGE tokenizer has no <pad> token")
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(8, os.cpu_count() or 8)
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        input_names = {item.name for item in session.get_inputs()}
        encoded = tokenizer.encode_batch(
            [(row["claim_text"], row["fact_text"]) for row in rows]
        )
        # The exported model changes scores when padding length changes, even with a
        # correct attention mask. Stable length buckets keep each pair in the same batch.
        order = sorted(
            range(len(rows)),
            key=lambda index: (
                len(encoded[index].ids), rows[index]["claim_id"], rows[index]["fact_id"]
            ),
        )
        scores = [0.0] * len(rows)
        for start in range(0, len(order), batch_size):
            indices = order[start:start + batch_size]
            batch = [encoded[index] for index in indices]
            max_length = max(len(item.ids) for item in batch)
            input_ids = np.full((len(batch), max_length), pad_id, dtype=np.int64)
            attention_mask = np.zeros((len(batch), max_length), dtype=np.int64)
            for position, item in enumerate(batch):
                length = len(item.ids)
                input_ids[position, :length] = item.ids
                attention_mask[position, :length] = item.attention_mask
            feed = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            output = session.run(None, {key: value for key, value in feed.items() if key in input_names})[0]
            for index, value in zip(indices, output.reshape(-1)):
                scores[index] = float(value)
        return scores

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but PyTorch cannot access a CUDA device")
    if device == "cpu":
        torch.set_num_threads(min(8, torch.get_num_threads()))
    model_dir = MODELS[model_name]
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    model.to(torch.device(device))
    model.eval()
    scores = []
    with torch.no_grad():
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            encoded = tokenizer(
                [row["claim_text"] for row in batch],
                [row["fact_text"] for row in batch],
                padding=True,
                truncation=True,
                max_length=128,
                return_tensors="pt",
            )
            encoded = encoded.to(torch.device(device))
            logits = model(**encoded).logits
            scores.extend((logits[:, 1] - logits[:, 0]).cpu().tolist())
    return [float(value) for value in scores]


def score_map(rows: list[dict], scores: list[float]) -> dict[tuple[str, str], float]:
    return {(row["claim_id"], row["fact_id"]): score for row, score in zip(rows, scores)}


def rank(group: list[dict], scores: dict[tuple[str, str], float]) -> tuple[list[dict], bool]:
    ordered = sorted(group, key=lambda row: (-scores[(row["claim_id"], row["fact_id"])], row["fact_id"]))
    k = int(group[0]["expected_k"])
    tied_at_cutoff = False
    if len(ordered) > k:
        left = scores[(ordered[k - 1]["claim_id"], ordered[k - 1]["fact_id"])]
        right = scores[(ordered[k]["claim_id"], ordered[k]["fact_id"])]
        tied_at_cutoff = math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-7)
    return ordered[:k], tied_at_cutoff


def evaluate(rows: list[dict], scores: dict[tuple[str, str], float]) -> dict:
    grouped = group_rows(rows)
    by_task: dict[str, list[dict]] = defaultdict(list)
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for group in grouped.values():
        by_task[group[0]["task_type"]].append(group)
        by_variant[group[0]["claim_variant"]].append(group)

    results = {}
    for task_type, groups in by_task.items():
        correct = complete = ambiguous = total_gold = 0
        source_recall = source_precision = 0.0
        for group in groups:
            chosen, tied = rank(group, scores)
            gold = set(group[0]["gold_fact_ids"])
            predicted = {row["fact_id"] for row in chosen}
            hits = len(predicted & gold)
            total_gold += len(gold)
            source_recall += hits / len(gold)
            source_precision += hits / len(predicted) if predicted else 0.0
            ambiguous += int(tied)
            if task_type == "growth_set":
                complete += int(predicted == gold and not tied)
            else:
                correct += int(len(chosen) == 1 and chosen[0]["label"] == 1 and not tied)
        if task_type == "growth_set":
            results[task_type] = {
                "claims": len(groups),
                "source_set_exact_match": complete / max(1, len(groups)),
                "source_set_completeness_recall": source_recall / max(1, len(groups)),
                "source_set_precision": source_precision / max(1, len(groups)),
                "ambiguous_at_top2_boundary": ambiguous,
            }
        else:
            results[task_type] = {
                "claims": len(groups),
                "top1_accuracy": correct / max(1, len(groups)),
                "error_association_rate": 1 - correct / max(1, len(groups)),
                "ambiguous_top1": ambiguous,
            }

    variant_results = {}
    for variant, groups in by_variant.items():
        quote_groups = [group for group in groups if group[0]["task_type"] != "growth_set"]
        growth_groups = [group for group in groups if group[0]["task_type"] == "growth_set"]
        quote_correct = 0
        growth_exact = 0
        for group in quote_groups:
            chosen, tied = rank(group, scores)
            quote_correct += int(chosen[0]["label"] == 1 and not tied)
        for group in growth_groups:
            chosen, tied = rank(group, scores)
            growth_exact += int({row["fact_id"] for row in chosen} == set(group[0]["gold_fact_ids"]) and not tied)
        variant_results[variant] = {
            "quote_claims": len(quote_groups),
            "quote_top1_accuracy": quote_correct / max(1, len(quote_groups)),
            "growth_claims": len(growth_groups),
            "growth_set_exact_match": growth_exact / max(1, len(growth_groups)),
        }

    return {"by_task": results, "by_variant": variant_results}


def deterministic_baselines(rows: list[dict]) -> dict:
    grouped = group_rows(rows)
    result = {"quote_current_prior": {}, "growth_set": {}}
    quote_groups = [group for group in grouped.values() if group[0]["task_type"] != "growth_set"]
    growth_groups = [group for group in grouped.values() if group[0]["task_type"] == "growth_set"]

    alias_to_metric = {alias: metric for metric, alias in CONTROLLED_ALIASES.items()}

    def resolve_metric(group: list[dict], aliases: bool = False) -> str | None:
        claim_text = group[0]["claim_text"]
        phrases = {row["metric"] for row in group}
        if aliases:
            phrases.update(alias_to_metric)
        matched = [phrase for phrase in phrases if phrase in claim_text]
        if not matched:
            return None
        phrase = min(matched, key=lambda item: (-len(item), item))
        return alias_to_metric.get(phrase, phrase)

    def literal_matches(group: list[dict]) -> list[dict]:
        metric = resolve_metric(group)
        claim_text = group[0]["claim_text"]
        return [row for row in group if metric and row["metric"] == metric and row["period"] in claim_text]

    def alias_aware_matches(group: list[dict]) -> list[dict]:
        claim_text = group[0]["claim_text"]
        metric = resolve_metric(group, aliases=True)
        return [row for row in group if row["metric"] == metric and row["period"] in claim_text]

    literal_resolved = [literal_matches(group) for group in quote_groups]
    literal_answered = [matches for matches in literal_resolved if len(matches) == 1]
    literal_correct = sum(int(matches[0]["label"] == 1) for matches in literal_answered)
    alias_aware_resolved = [alias_aware_matches(group) for group in quote_groups]
    alias_aware_answered = [matches for matches in alias_aware_resolved if len(matches) == 1]
    alias_aware_correct = sum(int(matches[0]["label"] == 1) for matches in alias_aware_answered)
    period_candidate_counts = [
        sum(row["period"] == group[0]["target_period"] for row in group)
        for group in quote_groups
    ]

    def period_fixed_choice(group: list[dict], use_current: bool) -> dict | None:
        periods = [row["period"] for row in group]
        chosen_period = max(periods) if use_current else min(periods)
        matching = [row for row in group if row["period"] == chosen_period]
        return min(matching, key=lambda row: row["fact_id"]) if matching else None

    def position_choice(group: list[dict], position: str) -> dict:
        if position == "first":
            return min(group, key=lambda row: row["candidate_position"])
        return max(group, key=lambda row: row["candidate_position"])

    position_baselines = {
        name: sum(int(position_choice(group, position)["label"]) for group in quote_groups)
        / max(1, len(quote_groups))
        for name, position in (("first_in_seeded_order", "first"), ("last_in_seeded_order", "last"))
    }
    fixed_period_baselines = {
        name: sum(
            int(picked is not None and picked["label"])
            for group in quote_groups
            for picked in [period_fixed_choice(group, use_current)]
        ) / max(1, len(quote_groups))
        for name, use_current in (("always_current_period_with_stable_tiebreak", True),
                                  ("always_prior_period_with_stable_tiebreak", False))
    }

    result["quote_current_prior"] = {
        **position_baselines,
        **fixed_period_baselines,
        "period_only_unique_resolution_rate": sum(count == 1 for count in period_candidate_counts)
        / max(1, len(period_candidate_counts)),
        "period_only_candidate_count_mean": sum(period_candidate_counts)
        / max(1, len(period_candidate_counts)),
        "period_only_random_tie_expected_top1": sum(
            1 / count for count in period_candidate_counts if count
        ) / max(1, len(period_candidate_counts)),
        "literal_metric_and_period_rule": {
            "claims": len(quote_groups),
            "answered": len(literal_answered),
            "coverage": len(literal_answered) / max(1, len(quote_groups)),
            "top1_accuracy_on_answered": literal_correct / max(1, len(literal_answered)),
            "correct_over_all_claims": literal_correct / max(1, len(quote_groups)),
        },
        "controlled_alias_lexicon_and_period_rule": {
            "claims": len(quote_groups),
            "answered": len(alias_aware_answered),
            "coverage": len(alias_aware_answered) / max(1, len(quote_groups)),
            "top1_accuracy_on_answered": alias_aware_correct / max(1, len(alias_aware_answered)),
            "correct_over_all_claims": alias_aware_correct / max(1, len(quote_groups)),
            "lexicon_entries": len(CONTROLLED_ALIASES),
        },
        "random_choice_expected_accuracy": sum(
            1 / len(group) for group in quote_groups
        ) / max(1, len(quote_groups)),
    }
    pos0 = sum(
        next(row for row in group if row["candidate_position"] == 0)["label"]
        for group in quote_groups
    )
    result["quote_current_prior"]["first_position_gold_rate"] = pos0 / max(1, len(quote_groups))

    first2_exact = last2_exact = literal_metric_exact = alias_aware_exact = 0
    literal_metric_answered = alias_aware_answered = 0
    random_exact = 0.0
    for group in growth_groups:
        gold = set(group[0]["gold_fact_ids"])
        ordered = sorted(group, key=lambda row: row["candidate_position"])
        first2_exact += int({row["fact_id"] for row in ordered[:2]} == gold)
        last2_exact += int({row["fact_id"] for row in ordered[-2:]} == gold)
        literal_metric = resolve_metric(group)
        by_literal_metric = [row for row in group if literal_metric and row["metric"] == literal_metric]
        if by_literal_metric:
            literal_metric_answered += 1
            literal_metric_exact += int({row["fact_id"] for row in by_literal_metric} == gold)
        canonical_metric = resolve_metric(group, aliases=True)
        by_alias_aware_metric = [row for row in group if row["metric"] == canonical_metric]
        if by_alias_aware_metric:
            alias_aware_answered += 1
            alias_aware_exact += int({row["fact_id"] for row in by_alias_aware_metric} == gold)
        random_exact += 1 / math.comb(len(group), 2)
    result["growth_set"] = {
        "claims": len(growth_groups),
        "first_two_in_shuffled_order_exact_match": first2_exact / max(1, len(growth_groups)),
        "last_two_in_shuffled_order_exact_match": last2_exact / max(1, len(growth_groups)),
        "literal_metric_set_rule": {
            "claims": len(growth_groups),
            "answered": literal_metric_answered,
            "coverage": literal_metric_answered / max(1, len(growth_groups)),
            "source_set_exact_match_on_answered": literal_metric_exact / max(1, literal_metric_answered),
            "exact_over_all_claims": literal_metric_exact / max(1, len(growth_groups)),
        },
        "controlled_alias_lexicon_set_rule": {
            "answered": alias_aware_answered,
            "coverage": alias_aware_answered / max(1, len(growth_groups)),
            "source_set_exact_match_on_answered": alias_aware_exact / max(1, alias_aware_answered),
            "exact_over_all_claims": alias_aware_exact / max(1, len(growth_groups)),
            "lexicon_entries": len(CONTROLLED_ALIASES),
        },
        "random_two_choice_expected_exact_match": random_exact / max(1, len(growth_groups)),
    }
    return result


def shuffled_rows(rows: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    grouped = group_rows(rows)
    result = []
    for claim_id in sorted(grouped):
        group = list(grouped[claim_id])
        rng.shuffle(group)
        result.extend(group)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "答辩评测" / "annual_benchmark_repaired_20261004_v2" / "benchmark.jsonl")
    parser.add_argument("--model", choices=tuple(MODELS), required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--shuffle-seed", type=int, default=73)
    parser.add_argument("--order-check-claims-per-task", type=int, default=16)
    parser.add_argument("--rescore-full-after-shuffle", action="store_true")
    args = parser.parse_args()
    rows = read_rows(args.dataset)
    start = time.perf_counter()
    scores = score_rows(rows, args.model, args.batch_size, args.device)
    primary = score_map(rows, scores)
    first_metrics = evaluate(rows, primary)
    shuffled = shuffled_rows(rows, args.shuffle_seed)
    if args.rescore_full_after_shuffle:
        shuffled_scores = score_rows(shuffled, args.model, args.batch_size, args.device)
        reordered = score_map(shuffled, shuffled_scores)
    else:
        reordered = primary
    second_metrics = evaluate(shuffled, reordered)
    score_delta = (
        max(abs(primary[key] - reordered[key]) for key in primary)
        if args.rescore_full_after_shuffle and primary else None
    )
    original_groups = group_rows(rows)
    shuffled_groups = group_rows(shuffled)
    changed = 0
    for claim_id, group in original_groups.items():
        first_choice, _ = rank(group, primary)
        second_choice, _ = rank(shuffled_groups[claim_id], reordered)
        if {row["fact_id"] for row in first_choice} != {row["fact_id"] for row in second_choice}:
            changed += 1

    by_task_groups: dict[str, list[str]] = defaultdict(list)
    for claim_id, group in original_groups.items():
        by_task_groups[group[0]["task_type"]].append(claim_id)
    rng = random.Random(args.shuffle_seed)
    sample_ids = []
    for task_type in sorted(by_task_groups):
        candidates = sorted(by_task_groups[task_type])
        rng.shuffle(candidates)
        sample_ids.extend(candidates[:max(0, args.order_check_claims_per_task)])
    sample_id_set = set(sample_ids)
    sample_rows = [row for row in rows if row["claim_id"] in sample_id_set]
    sample_shuffled = shuffled_rows(sample_rows, args.shuffle_seed + 1)
    sample_before_scores = score_rows(sample_rows, args.model, args.batch_size, args.device)
    sample_after_scores = score_rows(sample_shuffled, args.model, args.batch_size, args.device)
    sample_before = score_map(sample_rows, sample_before_scores)
    sample_after = score_map(sample_shuffled, sample_after_scores)
    sample_max_delta = max(
        (abs(sample_before[key] - sample_after[key]) for key in sample_before),
        default=0.0,
    )
    sample_changed = 0
    sample_before_groups = group_rows(sample_rows)
    sample_after_groups = group_rows(sample_shuffled)
    for claim_id, group in sample_before_groups.items():
        before, _ = rank(group, sample_before)
        after, _ = rank(sample_after_groups[claim_id], sample_after)
        sample_changed += int(
            {row["fact_id"] for row in before} != {row["fact_id"] for row in after}
        )
    output = {
        "model": args.model,
        "model_path": str(MODELS[args.model]),
        "device": "CPUExecutionProvider" if args.model == "bge" else args.device,
        "batch_size": args.batch_size,
        "batching": "stable_length_bucketed" if args.model == "bge" else "input_order_dynamic_padding",
        "dataset": str(args.dataset),
        "programmatic_labels": True,
        "rows": len(rows),
        "claims": len(original_groups),
        "companies": len({row["company"] for row in rows}),
        "reports": len({row["source_file"] for row in rows}),
        "baselines": deterministic_baselines(rows),
        "model_metrics": first_metrics,
        "candidate_order_invariance": {
            "shuffle_seed": args.shuffle_seed,
            "full_rank_aggregation_uses_cached_pair_scores": not args.rescore_full_after_shuffle,
            "full_rank_aggregation_prediction_set_changed_claims": changed,
            "full_rank_aggregation_prediction_set_consistency": 1 - changed / max(1, len(original_groups)),
            "full_rescore_after_shuffle": args.rescore_full_after_shuffle,
            "max_absolute_score_delta": score_delta,
            "before": first_metrics,
            "after": second_metrics,
            "rescore_sample": {
                "claims_per_task_requested": args.order_check_claims_per_task,
                "claims": len(sample_before_groups),
                "rows": len(sample_rows),
                "max_absolute_score_delta": sample_max_delta,
                "prediction_set_changed_claims": sample_changed,
                "prediction_set_consistency": 1 - sample_changed / max(1, len(sample_before_groups)),
            },
        },
        "elapsed_seconds_evaluation": round(time.perf_counter() - start, 2),
    }
    out = args.dataset.parent / f"metrics_{args.model}.json"
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
