"""Measure routing latency on the semantic rewrite evaluation set.

The pure-API timing is taken from the completed real DeepSeek replay when no
API key is configured locally. The routed path measures rule + local BGE time
directly and reuses the same recorded API responses for decision replay. If a
DEEPSEEK_API_KEY is configured, pass --live-api to measure the routed fallback
request as well. Keys are never written to output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT))


def rows(path: Path):
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def measure_live_api(claims, facts):
    from zhilian import llm
    calls = []
    for start in range(0, len(claims), 40):
        batch = claims[start:start + 40]
        t0 = time.perf_counter()
        suggestions = llm.suggest_links(batch, facts)
        elapsed = (time.perf_counter() - t0) * 1000
        calls.append({"claims": len(batch), "suggestions": len(suggestions),
                      "latency_ms": round(elapsed, 1)})
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-api", action="store_true",
                    help="also call the configured DeepSeek key for the routed fallback")
    ap.add_argument("--output", type=Path,
                    default=ROOT / "pure_api_vs_routed_latency.json")
    args = ap.parse_args()

    from zhilian import engine
    from zhilian.office import read_facts
    from zhilian.reranker import score_pairs, status

    data = rows(ROOT / "semantic_rewrite_eval_v2.jsonl")
    api_stats = json.loads((ROOT / "semantic_rewrite_eval_v2_api_stats.json").read_text(encoding="utf-8"))
    api_predictions = rows(ROOT / "semantic_rewrite_eval_v2_api_predictions.jsonl")
    facts = read_facts(PROJECT / "长文Word测试" / "配套数据_初始.xlsx", "facts")
    unique = {}
    for item in data:
        unique.setdefault(item["claim_id"], item)
    claims = [{"id": item["claim_id"], "kind": "quote", "original": item["claim_text"],
               "refs": [], "confirmed": False} for item in unique.values()]

    pure_calls = api_stats.get("calls", [])
    pure_total_ms = round(sum(float(c.get("latency_ms", 0)) for c in pure_calls), 1)

    # Measure the production gate, including rule matching and local BGE.
    t0 = time.perf_counter()
    route_counts = {"rule_unique": 0, "local_confident": 0,
                    "api_fallback": 0, "abstain": 0}
    fallback = []
    local_claim_ms = []
    for claim in claims:
        rule = engine.best_facts(claim["original"], facts)
        if len(rule) == 1:
            route_counts["rule_unique"] += 1
            continue
        local_t0 = time.perf_counter()
        scored = score_pairs(claim["original"], facts, batch_size=16)
        local_claim_ms.append((time.perf_counter() - local_t0) * 1000)
        if not scored:
            route_counts["abstain"] += 1
            continue
        margin = scored[0]["raw_score"] - scored[1]["raw_score"] if len(scored) > 1 else 999.0
        if scored[0]["raw_score"] >= -3.0 and margin >= 0.10:
            route_counts["local_confident"] += 1
        else:
            route_counts["api_fallback"] += 1
            fallback.append(claim)
    local_and_rule_ms = round((time.perf_counter() - t0) * 1000, 1)

    routed_api_calls = []
    if args.live_api:
        from zhilian import llm
        if not llm.config()["enabled"]:
            raise SystemExit("--live-api requested but DEEPSEEK_API_KEY is not configured")
        routed_api_calls = measure_live_api(fallback, facts)
    else:
        # Existing real replay is used only to provide the measured network
        # baseline. No network call or secret is made in replay mode.
        routed_api_calls = [{"claims": len(fallback), "source": "replay-baseline",
                             "latency_ms": None}]
    routed_api_ms = sum(float(c["latency_ms"]) for c in routed_api_calls
                        if c.get("latency_ms") is not None)
    result = {
        "dataset_claims": len(claims),
        "local_model": status(),
        "pure_api_real_replay": {
            "batches": len(pure_calls), "claims": len(claims),
            "batch_latencies_ms": [c.get("latency_ms") for c in pure_calls],
            "total_ms": pure_total_ms, "total_seconds": round(pure_total_ms / 1000, 3),
        },
        "routed": {
            "route_counts": route_counts,
            "rule_and_local_ms": local_and_rule_ms,
            "rule_and_local_seconds": round(local_and_rule_ms / 1000, 3),
            "api_calls": routed_api_calls,
            "api_ms": round(routed_api_ms, 1),
            "total_seconds": round((local_and_rule_ms + routed_api_ms) / 1000, 3)
            if routed_api_ms else None,
            "api_timing_status": "live" if args.live_api else "not_measured_local_key_missing",
        },
        "caveats": [
            "纯 API 时间来自已完成的真实 DeepSeek 72 条回放。",
            "无本地 API Key 时，路由 API 部分不重复联网；只测量规则 + 本地 BGE，保留实际纯 API 网络基线。",
            "端到端路由总耗时只有在 --live-api 下才是同机同次实测。",
        ],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
