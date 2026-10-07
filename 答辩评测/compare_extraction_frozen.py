"""Paired extraction benchmark: same PDF text, splitter and denominator for both engines."""
import csv
import hashlib
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zhilian import engine

OUT = ROOT / '答辩评测/optimization_20261001'


def main():
    spec = importlib.util.spec_from_file_location('baseline', OUT / 'baseline_engine.py')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    cache = OUT / 'numeric_fragments.jsonl'
    if not cache.exists():
        fragments = []
        for path in sorted((ROOT / '答辩评测/cn_reports').glob('*.pdf')):
            with pymupdf.open(path) as document:
                for page in range(9, min(45, len(document))):
                    text = document[page].get_text('text')
                    for i, fragment in enumerate(re.split(r'[。！？；;\n]', text)):
                        fragment = fragment.strip()
                        if re.search(r'\d', fragment):
                            fragments.append({'file': path.name, 'page': page + 1,
                                              'fragment': i, 'text': fragment})
        cache.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in fragments),
                         encoding='utf-8')
    rows = [json.loads(line) for line in cache.read_text(encoding='utf-8').splitlines()]
    results, details, added = [], [], []
    for name, module in [('before', baseline), ('after', engine)]:
        started = time.perf_counter()
        counts = Counter()
        by_file = {}
        for row in rows:
            block = {'file_id': row['file'], 'location': ['pdf', row['page'], row['fragment']],
                     'label': f"PDF p{row['page']}", 'text': row['text']}
            claims = module.extract_claims(block, [])
            info = by_file.setdefault(row['file'], {'numeric_fragments': 0, 'recognized': 0,
                                                    'claims': 0})
            info['numeric_fragments'] += 1
            info['recognized'] += bool(claims)
            info['claims'] += len(claims)
            counts.update(c['kind'] for c in claims)
            if name == 'after':
                old_claims = baseline.extract_claims(block, [])
                old_spans = {(c['kind'], c['start'], c['end']) for c in old_claims}
                new_claims = [c for c in claims if (c['kind'], c['start'], c['end']) not in old_spans]
                if new_claims:
                    added.append({**row, 'claims': new_claims,
                                  'label_basis': 'newly recognized candidates, not annotated gold'})
        totals = {'system': name, 'numeric_fragments': len(rows),
                  'recognized': sum(r['recognized'] for r in by_file.values()),
                  'claims': sum(counts.values()), 'kinds': dict(counts),
                  'evaluation_seconds': round(time.perf_counter() - started, 3)}
        totals['fragment_hit_rate'] = totals['recognized'] / len(rows)
        results.append(totals)
        details.extend({'system': name, 'file': f, **r} for f, r in by_file.items())
    report = {'denominator': 'PDF numeric text fragments (including table fragments), not eligible assertions',
              'extractor': 'PyMuPDF text layer; page 10-45; fixed cached text',
              'corpus_sha256': hashlib.sha256(cache.read_bytes()).hexdigest(),
              'source_hashes': {'baseline': hashlib.sha256((OUT / 'baseline_engine.py').read_bytes()).hexdigest(),
                                'after': hashlib.sha256((ROOT / 'zhilian/engine.py').read_bytes()).hexdigest()},
              'metrics': results, 'new_candidate_fragments': len(added),
              'warnings': ['This is fragment coverage, not precision/recall or human truth.',
                           'After evaluation time also includes baseline comparison; not a latency comparison.']}
    (OUT / 'extraction_comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'new_claims_for_review.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in added), encoding='utf-8')
    with (OUT / 'extraction_per_report.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(details[0]))
        writer.writeheader()
        writer.writerows(details)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
