"""核实年报修正版训练数据的本期/上期配平（独立于模型 manifest 自报）。"""
import json
import pathlib
import re
from collections import Counter, defaultdict

D = pathlib.Path('答辩评测/annual_reports_weak_training_repaired_20261004')
for name in ('train.jsonl', 'dev.jsonl', 'test.jsonl'):
    p = D / name
    if not p.is_file():
        continue
    rows = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
    print(f'=== {name}  {len(rows)} 行 ===')
    print('  字段:', sorted(rows[0].keys()))
    # 按 label=1 的期间分布
    per = Counter()
    task = Counter()
    for r in rows:
        if r.get('label') == 1:
            t = r.get('task_type') or r.get('claim_variant') or '（无）'
            task[t] += 1
            ft = r.get('fact_text') or ''
            m = re.search(r'period=([^；]*)', ft)
            per[m.group(1) if m else '（无 period）'] += 1
    print(f'  正例按 task_type : {dict(task)}')
    print(f'  正例按 period    : {dict(per)}')
    # 题数
    qs = defaultdict(list)
    for r in rows:
        qs[r.get('claim_id')].append(r)
    qt = Counter(v[0].get('task_type') or v[0].get('claim_variant') or '（无）' for v in qs.values())
    print(f'  题数按 task_type : {dict(qt)}')
    print()

p = D / 'manifest.json'
if p.is_file():
    print('=== 数据 manifest ===')
    print(p.read_text(encoding='utf-8')[:1200])
