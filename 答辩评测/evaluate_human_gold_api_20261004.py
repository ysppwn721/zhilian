"""Live pure-API evaluation on the frozen 150-claim human-gold set.

The key must be supplied through DEEPSEEK_API_KEY by the caller. It is never
written to output. Claims are greedily batched under the production limits of
40 claims and 150 facts per request.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "答辩评测" / "human_gold_eval_20261004"
DATA = REVIEW / "human_gold_20261004.jsonl"
OUT = REVIEW / "human_gold_api_predictions.jsonl"
STATS = REVIEW / "human_gold_api_stats.json"
METRICS = REVIEW / "human_gold_api_metrics.json"
ERROR = REVIEW / "human_gold_api_error.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_local_env() -> None:
    """Load the ignored experiment env file without echoing secret values."""
    path = Path(__file__).with_name("human_gold_api.env")
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # An explicitly exported process variable wins over the file.
        if key not in os.environ or not os.environ[key].strip():
            os.environ[key] = value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rows() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]


def batches(rows: list[dict]) -> list[tuple[list[dict], list[dict]]]:
    claims: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        claims[row["claim_id"]].append(row)
    by_company: dict[str, list[str]] = defaultdict(list)
    for claim_id, group in claims.items():
        by_company[group[0]["company"]].append(claim_id)
    result: list[tuple[list[dict], list[dict]]] = []
    current_claims: list[str] = []
    current_facts: dict[str, dict] = {}
    def flush() -> None:
        nonlocal current_claims, current_facts
        if current_claims:
            result.append(([claims[cid][0] for cid in current_claims], list(current_facts.values())))
            current_claims, current_facts = [], {}
    for company in sorted(by_company):
        company_claims = by_company[company]
        company_facts = {row["fact_id"]: row for cid in company_claims for row in claims[cid]}
        if current_claims and (len(current_claims) + len(company_claims) > 40 or
                               len(current_facts) + len(company_facts) > 150):
            flush()
        current_claims.extend(company_claims)
        current_facts.update(company_facts)
    flush()
    return result


def evaluate(rows: list[dict], predictions: dict[str, list[str]]) -> dict:
    claims: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        claims[row["claim_id"]].append(row)
    by_task: dict[str, dict[str, int]] = {}
    for cid, group in claims.items():
        task = group[0]["task_type"]
        item = by_task.setdefault(task, {"claims": 0, "correct": 0, "answered": 0, "errors": 0})
        item["claims"] += 1
        gold = set(group[0]["gold_fact_ids"])
        refs = set(predictions.get(cid, []))
        if refs:
            item["answered"] += 1
            if refs == gold:
                item["correct"] += 1
            else:
                item["errors"] += 1
    result = {"claims": len(claims), "by_task": {}, "total_calls": None}
    for task, item in by_task.items():
        result["by_task"][task] = {
            **item,
            "exact_or_top1": item["correct"] / max(1, item["claims"]),
            "coverage": item["answered"] / max(1, item["claims"]),
            "abstain_rate": 1 - item["answered"] / max(1, item["claims"]),
            "error_association_rate": item["errors"] / max(1, item["claims"]),
        }
    return result


def main() -> int:
    load_local_env()
    from zhilian import llm
    if not llm.config()["enabled"]:
        ERROR.write_text(json.dumps({
            "experiment": "human_gold_pure_api_20261004",
            "status": "not_configured",
            "message": "DEEPSEEK_API_KEY is not configured",
            "api_key_written": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit("DEEPSEEK_API_KEY is not configured; set it in the process environment or human_gold_api.env and retry")
    rows = load_rows()
    if len({row["claim_id"] for row in rows}) != 150:
        raise SystemExit("expected frozen 150-claim human-gold dataset")
    predictions: dict[str, list[str]] = {}
    call_stats = []
    for index, (claim_rows, fact_rows) in enumerate(batches(rows), 1):
        claims = [{"id": row["claim_id"], "kind": "growth" if row["task_type"] == "growth_set" else "quote",
                   "original": row["claim_text"], "refs": [], "confirmed": False} for row in claim_rows]
        facts = [{key: row.get(key, "") for key in ("id", "subject", "metric", "period", "unit", "scope")} for row in fact_rows]
        started = time.perf_counter()
        try:
            with llm.using_mode("api_only"):
                suggestions = llm.suggest_links(claims, facts)
        except Exception as exc:
            usage = llm.consume_last_call_metrics() or {}
            ERROR.write_text(json.dumps({
                "experiment": "human_gold_pure_api_20261004",
                "status": "request_failed",
                "batch": index,
                "claims": len(claims),
                "facts": len(facts),
                "error_type": type(exc).__name__,
                "message": str(exc),
                "call_metrics": usage,
                "api_key_written": False,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            raise SystemExit(
                f"纯 API 实验在第 {index} 批失败（{type(exc).__name__}）。"
                f"诊断已写入 {ERROR}；请检查网络、端点和额度后重试。"
            ) from exc
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        usage = llm.consume_last_call_metrics() or {}
        by_claim = {item["claim_id"]: item for item in suggestions}
        for claim in claims:
            refs = by_claim.get(claim["id"], {}).get("refs", [])
            predictions[claim["id"]] = list(dict.fromkeys(refs)) if isinstance(refs, list) else []
        call_stats.append({"batch": index, "claims": len(claims), "facts": len(facts),
                           "suggestions": len(suggestions), "latency_ms": elapsed,
                           "usage": usage})
        print(f"batch {index}: claims={len(claims)} facts={len(facts)} latency={elapsed}ms")
    OUT.write_text("\n".join(json.dumps({"claim_id": cid, "refs": refs, "source": "deepseek"}, ensure_ascii=False)
                              for cid, refs in sorted(predictions.items())) + "\n", encoding="utf-8")
    metrics = evaluate(rows, predictions)
    metrics["total_calls"] = len(call_stats)
    stats = {"experiment": "human_gold_pure_api_20261004", "model": llm.config()["model"],
             "dataset_sha256": sha256(DATA), "claims": 150, "batches": call_stats,
             "api_key_written": False, "metrics": metrics}
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    METRICS.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
