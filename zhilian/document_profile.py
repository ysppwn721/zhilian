"""Deterministic document profile detection used for local-model selection.

This classifier never sends document text to a model. It only uses filenames,
document kinds and already extracted text, and returns evidence for the audit.
"""
from __future__ import annotations

import re


ANNUAL_NAME = re.compile(r"年报|年度报告|annual[ _-]?report|annual[ _-]?statement", re.I)
# Public-company annual reports are often downloaded with a normalized name
# such as ``000543_2018_皖能电力.pdf`` and lose the original "年报" suffix.
# This is only a filename signal; it must still be combined with report/audit
# language before selecting the annual reranker.
FINANCIAL_PDF_NAME = re.compile(r"^\d{6}[_-]\d{4}(?:[_-].+)?\.pdf$", re.I)
ANNUAL_TEXT = (
    ("annual_report_phrase", re.compile(r"年度报告|年度报告全文|annual report", re.I), 3),
    ("report_period", re.compile(r"报告期内|报告期末|本报告期|上年同期"), 1),
    ("accounting_statement", re.compile(r"合并资产负债表|合并利润表|现金流量表|资产负债表"), 2),
    ("listed_company_sections", re.compile(r"董事会报告|主要会计数据|经营情况讨论与分析|重要提示"), 2),
    ("audit_language", re.compile(r"审计报告|会计师事务所|归属于上市公司股东"), 1),
)


def detect(documents: list[dict], blocks: list[dict]) -> dict:
    scores = {"filename": 0, "text": 0, "structure": 0}
    reasons: list[str] = []
    names = [str(item.get("name") or "") for item in documents]
    name_hits = [name for name in names if ANNUAL_NAME.search(name)]
    financial_pdf_hits = [name for name in names if FINANCIAL_PDF_NAME.search(name)]
    if name_hits:
        scores["filename"] = min(3, len(name_hits))
        reasons.append(f"文件名命中年报标识 {len(name_hits)} 个")

    text = "\n".join(str(block.get("text") or "") for block in blocks)[:500_000]
    for label, pattern, weight in ANNUAL_TEXT:
        if pattern.search(text):
            scores["text"] += weight
            reasons.append(label)
    kinds = {str(item.get("kind") or "").lower() for item in documents}
    if "xlsx" in kinds and len(blocks) >= 5:
        scores["structure"] += 1
        reasons.append("Office 事实表与多段正文同时存在")

    total = sum(scores.values())
    # Require an explicit annual name plus report language, or the common
    # normalized listed-company PDF name plus both period and audit signals.
    normalized_pdf = bool(financial_pdf_hits) and scores["text"] >= 2 and {
        "report_period", "audit_language"
    }.issubset({label for label, pattern, weight in ANNUAL_TEXT if pattern.search(text)})
    strong = (bool(name_hits) and scores["text"] >= 3) or normalized_pdf
    profile = "annual" if strong else "default"
    confidence = min(1.0, total / 9.0)
    return {
        "profile": profile,
        "confidence": round(confidence, 3),
        "scores": scores,
        "reasons": reasons[:12],
        "document_count": len(documents),
        "text_block_count": len(blocks),
        "annual_name_hits": len(name_hits),
        "financial_pdf_name_hits": len(financial_pdf_hits),
        "method": "deterministic_filename_and_text_signals_v1",
    }
