"""Optional local ONNX reranker for narrow fact-candidate ranking.

The reranker never receives the full fact table by default. Callers pass the
small candidate set produced by the deterministic matcher. Missing optional
dependencies or model files are reported as unavailable so the Agent can
fall back to rules or the remote model.
"""
from __future__ import annotations

import importlib.util
import math
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path


DEFAULT_MODEL = Path("models/bge-reranker-v2-m3-onnx-int8")
DEFAULT_ANNUAL_MODEL = Path("models/zh_reranker_bert_annual_repaired_v1_onnx_compat")
_PROFILE = ContextVar("zhilian_reranker_profile", default="default")

# numpy / onnxruntime / tokenizers 都是百毫秒级的重导入，而 status() 会在
# /api/health 与每次智能体路由决策时被调用。这里缓存"依赖是否可用"，
# 但不缓存 status() 本身——后者还依赖环境变量，测试会在运行中改动它们。
_OPTIONAL_DEPS = ('numpy', 'onnxruntime', 'tokenizers')
_IMPORT_LOCK = threading.Lock()
_IMPORTED = None


@contextmanager
def using_profile(profile: str):
    """Select a local model only for the current request/context."""
    if profile not in {"default", "annual"}:
        raise ValueError("无效的本地模型配置")
    token = _PROFILE.set(profile)
    try:
        yield
    finally:
        _PROFILE.reset(token)


def _model_dir() -> Path:
    profile = _PROFILE.get()
    if profile == "annual":
        value = os.getenv("ZHILIAN_ANNUAL_RERANKER_PATH", str(DEFAULT_ANNUAL_MODEL))
    else:
        value = os.getenv("ZHILIAN_LOCAL_RERANKER_PATH", str(DEFAULT_MODEL))
    return Path(value).expanduser()


@lru_cache(maxsize=1)
def _dependencies_available() -> bool:
    """可选依赖是否齐备。只查规格不导入，因此不会拖慢没有装模型的部署。"""
    try:
        return all(importlib.util.find_spec(name) is not None for name in _OPTIONAL_DEPS)
    except (ImportError, ValueError):
        return False


def _imports():
    """导入并缓存可选依赖；缺失时返回 None（调用方据此降级为规则模式）。"""
    global _IMPORTED
    if not _dependencies_available():
        return None
    if _IMPORTED is None:
        with _IMPORT_LOCK:
            if _IMPORTED is None:
                try:
                    import numpy as np
                    import onnxruntime as ort
                    from tokenizers import Tokenizer
                except ImportError:
                    return None
                _IMPORTED = (np, ort, Tokenizer)
    return _IMPORTED


def status(profile: str | None = None) -> dict:
    token = _PROFILE.set(profile) if profile else None
    try:
        return _status()
    finally:
        if token is not None:
            _PROFILE.reset(token)


def _status() -> dict:
    directory = _model_dir()
    profile = _PROFILE.get()
    configured = bool(os.getenv("ZHILIAN_LOCAL_RERANKER_PATH", "").strip()) or os.getenv(
        "ZHILIAN_LOCAL_RERANKER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    if profile == "annual":
        configured = bool(os.getenv("ZHILIAN_ANNUAL_RERANKER_PATH", "").strip()) or os.getenv(
            "ZHILIAN_ANNUAL_RERANKER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    # 未启用时不必探测文件系统与依赖：这是绝大多数部署的路径。
    if not configured:
        return {"enabled": False, "configured": False, "path": str(directory),
                "model": str(_find_model_file(directory)),
                "model_name": _model_label(_find_model_file(directory)),
                "profile": profile, "dependencies": _dependencies_available()}
    imported = _imports() is not None
    model = _find_model_file(directory)
    return {
        "enabled": configured and imported and model.exists() and _tokenizer_file(directory).exists(),
        "configured": configured,
        "path": str(directory),
        "model": str(model),
        "model_name": _model_label(model),
        "profile": profile,
        "dependencies": imported,
    }


def _fact_text(fact: dict) -> str:
    return "；".join(f"{key}={fact.get(key, '')}" for key in
                     ("subject", "metric", "period", "unit", "scope"))


@lru_cache(maxsize=2)
def _tokenizer_file(directory: Path) -> Path:
    candidates = (directory / "tokenizer.json", directory / "onnx" / "tokenizer.json")
    return next((path for path in candidates if path.exists()), candidates[0])


def _find_model_file(directory: Path) -> Path:
    requested = os.getenv(
        "ZHILIAN_ANNUAL_RERANKER_FILE" if _PROFILE.get() == "annual" else "ZHILIAN_LOCAL_RERANKER_FILE", ""
    ).strip()
    if requested:
        candidate = Path(requested)
        return candidate if candidate.is_absolute() else directory / candidate
    candidates = (
        directory / "onnx" / "model_int8.onnx",
        directory / "model_int8.onnx",
        directory / "model_quantized.onnx",
        directory / "model_fp32.onnx",
    )
    return next((path for path in candidates if path.exists()), candidates[0])


def _model_label(model: Path) -> str:
    configured = os.getenv(
        "ZHILIAN_ANNUAL_RERANKER_NAME" if _PROFILE.get() == "annual" else "ZHILIAN_LOCAL_RERANKER_NAME", ""
    ).strip()
    if configured:
        return configured[:120]
    lower = str(model).lower()
    if "annual_repaired" in lower or "annual" in lower:
        return "annual-repaired-bert"
    if "bge" in lower:
        return "bge-reranker-v2-m3"
    return model.stem


@lru_cache(maxsize=2)
def _runtime(model_path: str):
    model = Path(model_path)
    imported = _imports()
    if imported is None:
        raise RuntimeError("本地 reranker 依赖未安装，请安装 onnxruntime、tokenizers 和 numpy")
    np, ort, Tokenizer = imported
    model_directory = model.parent.parent if model.parent.name == "onnx" else model.parent
    tokenizer = Tokenizer.from_file(str(_tokenizer_file(model_directory)))
    tokenizer.enable_truncation(max_length=int(os.getenv("ZHILIAN_RERANKER_MAX_LENGTH", "512")))
    tokenizer.enable_padding(pad_id=0, pad_token="<pad>")
    providers = ort.get_available_providers()
    requested = os.getenv("ZHILIAN_LOCAL_RERANKER_DEVICE", "auto").lower()
    if requested == "cuda" and "CUDAExecutionProvider" not in providers:
        raise RuntimeError("已要求 CUDA，但当前 ONNX Runtime 没有 CUDAExecutionProvider")
    selected = ["CUDAExecutionProvider", "CPUExecutionProvider"] if (
        requested == "cuda" or requested == "auto" and "CUDAExecutionProvider" in providers
    ) else ["CPUExecutionProvider"]
    session = ort.InferenceSession(str(model), providers=selected)
    return np, tokenizer, session, selected


def _model_file() -> Path:
    directory = _model_dir()
    model = _find_model_file(directory)
    if model.exists():
        return model
    raise FileNotFoundError(f"未找到本地 reranker 模型：{directory}")


def score_pairs(claim_text: str, facts: list[dict], *, batch_size: int = 16) -> list[dict]:
    """Return descending scores for the already narrowed fact candidates."""
    if not facts:
        return []
    np, tokenizer, session, providers = _runtime(str(_model_file()))
    pairs = [(claim_text, _fact_text(fact)) for fact in facts]
    inputs = session.get_inputs()
    names = {item.name for item in inputs}
    results = []
    for start in range(0, len(pairs), max(1, batch_size)):
        encodings = tokenizer.encode_batch(pairs[start:start + batch_size])
        arrays = {
            "input_ids": np.asarray([encoding.ids for encoding in encodings], dtype=np.int64),
            "attention_mask": np.asarray([encoding.attention_mask for encoding in encodings], dtype=np.int64),
        }
        token_types = [encoding.type_ids for encoding in encodings]
        if "token_type_ids" in names:
            arrays["token_type_ids"] = np.asarray(token_types, dtype=np.int64)
        output = session.run(None, {name: value for name, value in arrays.items() if name in names})[0]
        # BGE ONNX returns one raw score per pair; the fine-tuned BERT export
        # returns two classification logits. Use the positive-vs-negative
        # margin for the latter so both models share the same downstream gate.
        if output.ndim == 2 and output.shape[1] >= 2:
            flat = (output[:, 1] - output[:, 0]).tolist()
        else:
            flat = output.reshape(-1).tolist()
        for fact, raw in zip(facts[start:start + batch_size], flat):
            score = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, float(raw)))))
            results.append({"fact_id": fact["id"], "score": round(score, 6), "raw_score": float(raw),
                            "provider": "local-reranker", "model": _model_label(_model_file()),
                            "providers": providers})
    return sorted(results, key=lambda item: item["score"], reverse=True)


def choose(scores: list[dict], *, min_score: float | None = None,
           min_margin: float | None = None) -> dict:
    """Apply calibrated confidence gates; low confidence becomes abstain."""
    if not scores:
        return {"action": "abstain", "refs": [], "reason": "没有候选事实"}
    profile = _PROFILE.get()
    threshold_name = ("ZHILIAN_ANNUAL_RERANKER_MIN_RAW_SCORE"
                      if profile == "annual" else "ZHILIAN_RERANKER_MIN_RAW_SCORE")
    margin_name = ("ZHILIAN_ANNUAL_RERANKER_MIN_MARGIN"
                   if profile == "annual" else "ZHILIAN_RERANKER_MIN_MARGIN")
    default_threshold = "-3.0" if profile == "annual" else "-3.0"
    threshold = float(os.getenv(threshold_name, default_threshold)) if min_score is None else min_score
    margin = float(os.getenv(margin_name, "0.10")) if min_margin is None else min_margin
    top = scores[0]
    second = scores[1]["raw_score"] if len(scores) > 1 else -999.0
    if top["raw_score"] < threshold or top["raw_score"] - second < margin:
        return {"action": "abstain", "refs": [], "reason": "候选分数或分差未达到校准阈值",
                "top_score": top["score"], "top_raw_score": top["raw_score"],
                "margin": round(top["raw_score"] - second, 6), "profile": profile,
                "model": top.get("model")}
    return {"action": "link", "refs": [top["fact_id"]], "reason": "本地 reranker 通过校准阈值",
            "top_score": top["score"], "top_raw_score": top["raw_score"],
            "margin": round(top["raw_score"] - second, 6), "profile": profile,
            "model": top.get("model")}
