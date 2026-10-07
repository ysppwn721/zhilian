"""Benchmark the optional PDF text/table/chart extraction path.

This is an external pressure experiment. It does not touch the frozen semantic
test set or feed generated facts into training. Labels are structural checks
from the report's own change-percentage column, not human truth.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zhilian.pdf_ingest import extract_pdf


def svg_report(summary: dict, out: Path) -> None:
    bars = [
        ("文本层页覆盖", summary["mean_text_layer_ratio"] * 100, "#2563eb", "%"),
        ("表格变动交叉校验", summary["change_check_pass_rate"] * 100, "#0f766e", "%"),
        ("表格产出文档覆盖", summary["docs_with_facts"] / max(summary["documents"], 1) * 100, "#d97706", "%"),
    ]
    width, height = 1100, 560
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="60" y="54" font-family="Microsoft YaHei, Arial" font-size="28" font-weight="700" fill="#172033">公开年报 PDF 结构化抽取压力测试</text>',
        '<text x="60" y="84" font-family="Microsoft YaHei, Arial" font-size="15" fill="#64748b">结构化检查，不代表人工真值；图表候选需 OCR 或人工复核</text>',
    ]
    base_y = 150
    for index, (label, value, color, unit) in enumerate(bars):
        y = base_y + index * 105
        parts.append(f'<text x="60" y="{y+27}" font-family="Microsoft YaHei, Arial" font-size="18" fill="#172033">{label}</text>')
        parts.append(f'<rect x="300" y="{y}" width="620" height="42" rx="9" fill="#e2e8f0"/>')
        parts.append(f'<rect x="300" y="{y}" width="{620*min(max(value,0),100)/100:.1f}" height="42" rx="9" fill="{color}"/>')
        parts.append(f'<text x="945" y="{y+29}" font-family="Arial" font-size="22" font-weight="700" fill="{color}">{value:.1f}{unit}</text>')
    footer_y = 500
    parts.append(f'<text x="60" y="{footer_y}" font-family="Microsoft YaHei, Arial" font-size="16" fill="#334155">样本：{summary["documents"]} 份公开年报 · {summary["pages"]} 页 · {summary["facts"]} 条结构化事实 · {summary["chart_candidates"]} 个图表候选页</text>')
    parts.append(f'<text x="60" y="{footer_y+28}" font-family="Microsoft YaHei, Arial" font-size="14" fill="#b45309">限制：扫描 PDF、复杂跨页表、矢量图表需要 OCR / 人工确认，不自动写入事实表。</text>')
    parts.append('</svg>')
    out.write_text(''.join(parts), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--reports', type=Path, default=Path('答辩评测/cn_reports'))
    parser.add_argument('--out', type=Path, default=Path('答辩评测/pdf_multimodal_eval'))
    parser.add_argument('--limit', type=int, default=0, help='仅处理前 N 份，0 表示全部')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    reports = sorted(args.reports.glob('*.pdf'))
    if args.limit:
        reports = reports[:args.limit]
    results = []
    all_facts = []
    all_checks = []
    timings = []
    for index, pdf in enumerate(reports, 1):
        print(f'[{index}/{len(reports)}] {pdf.name}', flush=True)
        try:
            result = extract_pdf(pdf)
            checks = [check for table in result['tables'] for check in table['validation']['checks']]
            reported = [check for check in checks if check['reported_change'] is not None]
            row = {
                'file': pdf.name,
                'pages': result['pages'],
                'chars': result['chars'],
                'text_layer_ratio': result['text_layer_ratio'],
                'candidate_table_pages': result['candidate_table_pages'],
                'tables': len(result['tables']),
                'facts': len(result['facts']),
                'facts_with_cross_check': sum(bool(fact.get('has_cross_check')) for fact in result['facts']),
                'chart_candidates': len(result['chart_candidates']),
                'change_checks': len(reported),
                'change_check_pass': sum(check['within_tolerance'] for check in reported),
                'time_ms': result['timing_ms']['end_to_end'],
                'status': 'ok',
            }
            results.append(row)
            all_facts.extend([{**fact, 'source_file': pdf.name} for fact in result['facts']])
            all_checks.extend([{**check, 'source_file': pdf.name} for check in reported])
            timings.append(row['time_ms'])
        except Exception as exc:
            results.append({'file': pdf.name, 'status': 'error', 'error': f'{type(exc).__name__}: {exc}'})
            print(f'  ERROR: {type(exc).__name__}: {exc}', flush=True)

    valid = [row for row in results if row.get('status') == 'ok']
    summary = {
        'documents': len(reports), 'processed': len(valid), 'errors': len(results) - len(valid),
        'pages': sum(row.get('pages', 0) for row in valid),
        'facts': len(all_facts), 'tables': sum(row.get('tables', 0) for row in valid),
        'facts_with_cross_check': sum(row.get('facts_with_cross_check', 0) for row in valid),
        'facts_cross_check_rate': (sum(row.get('facts_with_cross_check', 0) for row in valid) / len(all_facts)) if all_facts else 0,
        'chart_candidates': sum(row.get('chart_candidates', 0) for row in valid),
        'docs_with_facts': sum(row.get('facts', 0) > 0 for row in valid),
        'mean_text_layer_ratio': statistics.mean(row['text_layer_ratio'] for row in valid) if valid else 0,
        'change_checks': len(all_checks),
        'change_check_pass': sum(check['within_tolerance'] for check in all_checks),
        'change_check_pass_rate': (sum(check['within_tolerance'] for check in all_checks) / len(all_checks)) if all_checks else 0,
        'mean_time_ms': statistics.mean(timings) if timings else 0,
        'p50_time_ms': statistics.median(timings) if timings else 0,
        'label_basis': 'PDF table reported change column cross-check; structural/programmatic, not human truth',
        'chart_basis': 'page-level candidate detection; values are not claimed without OCR/chart parser',
    }
    (args.out / 'pdf_multimodal_eval_report.json').write_text(json.dumps({'summary': summary, 'files': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    with (args.out / 'pdf_multimodal_files.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        fields = ['file', 'pages', 'chars', 'text_layer_ratio', 'candidate_table_pages', 'tables', 'facts', 'facts_with_cross_check', 'chart_candidates', 'change_checks', 'change_check_pass', 'time_ms', 'status']
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)
    with (args.out / 'pdf_facts_sample.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        fields = ['source_file', 'id', 'metric', 'period', 'value', 'unit', 'scope', 'source_page', 'has_cross_check', 'cross_check_status']
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_facts[:500])
    with (args.out / 'pdf_change_checks.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        fields = ['source_file', 'source_page', 'table_number', 'row_number', 'metric', 'unit',
                  'calculated_change', 'reported_change', 'has_cross_check', 'cross_check_status', 'within_tolerance']
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_checks)
    svg_report(summary, args.out / 'pdf_multimodal_benchmark.svg')

    # Render evidence for one Chinese annual report with tables and one report
    # with known chart-like pages.  The images are for inspection, not labels.
    if reports:
        representative = next((p for p in reports if '603839' in p.name), reports[0])
        extract_pdf(representative, args.out / 'representative')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
