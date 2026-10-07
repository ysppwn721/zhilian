"""Create an auditable, heuristic taxonomy for failed PDF change checks.

The labels describe likely failure mechanisms for triage; they are not human
ground truth.  Every row keeps the source file/page/table/row for inspection.
"""
from __future__ import annotations

import csv
import json
import re
import argparse
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def classify(row: dict) -> tuple[str, str]:
    metric = row['metric']
    calculated = float(row['calculated_change']) if row['calculated_change'] else None
    reported = float(row['reported_change']) if row['reported_change'] else None
    if any(word in metric for word in ('占', '比例', '率')):
        return 'ratio_semantics', '指标本身是比例/占比，表内“变动比例”可能是百分点或相对变化，需明确口径'
    if reported is not None and abs(reported) > 1000:
        return 'change_column_selection', '报告值数量级像变动金额而非百分比，疑似变动金额/变动比例列错位'
    if calculated is not None and reported is not None and calculated * reported < 0:
        return 'sign_or_period_mismatch', '计算值与报告值符号相反，需检查上期列、负数括号或变动方向'
    if calculated is not None and reported is not None and abs(calculated - reported) > 100:
        return 'magnitude_mismatch', '计算值与报告值数量级差异大，需检查拆列数字、表头或单位'
    return 'other_structural_mismatch', '已发现差异，但现有字段不足以自动判定具体原因'


def render_examples(rows: list[dict], out: Path) -> list[dict]:
    examples = []
    for category in sorted({r['failure_category'] for r in rows}):
        row = next(r for r in rows if r['failure_category'] == category)
        source = ROOT / '答辩评测/cn_reports' / row['source_file']
        image = out / 'failure_examples' / f"{category}_p{row['source_page']}.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        try:
            with pymupdf.open(source) as document:
                page = document[int(row['source_page']) - 1]
                pix = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                pix.save(str(image))
            rendered = str(image.relative_to(ROOT))
        except Exception as exc:
            rendered = f'error: {type(exc).__name__}: {exc}'
        examples.append({**row, 'example_image': rendered})
    return examples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--eval-dir', type=Path,
                        default=ROOT / '答辩评测/pdf_multimodal_eval_v4')
    args = parser.parse_args()
    out = args.eval_dir.resolve()
    src = out / 'pdf_change_checks.csv'
    with src.open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    failed = [row for row in rows if row['within_tolerance'].lower() == 'false']
    for row in failed:
        row['failure_category'], row['failure_reason'] = classify(row)
    examples = render_examples(failed, out)
    counts = Counter(row['failure_category'] for row in failed)
    payload = {
        'source': str(src.relative_to(ROOT)),
        'failed_rows': len(failed),
        'categories': dict(sorted(counts.items())),
        'label_basis': 'heuristic triage for engineering; not human truth',
        'examples': examples,
    }
    (out / 'pdf_failure_taxonomy.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = list(failed[0]) if failed else []
    with (out / 'pdf_failure_taxonomy.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(failed)
    print(json.dumps({'failed_rows': len(failed), 'categories': dict(counts)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
