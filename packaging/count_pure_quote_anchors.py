"""统计 v3 池里"纯单值锚点"的真实上限，决定 quote 层能做到多大。

纯单值锚点 = 句子含 fact 的真实数值 + 无任何增长/对比标志 + 指标名在句中。
这类句子是 v3 单值题的唯一真实来源（计划 P1-3 要求单值题必须含真实数值）。
"""
import json
import pathlib
import re
from collections import Counter, defaultdict

D = pathlib.Path('答辩评测/annual_reports_v3_pool')
GROWTH = re.compile(r'同比|较上年|较上期|与上年同期|增减|变动|增长|下降|上升|减少|增加|增幅|降幅|提高|降低|百分点')
FACT = {'元': 1.0, '千元': 1e3, '万元': 1e4, '亿元': 1e8}


def canon(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').strip('()')
    try:
        return round(float(x.rstrip('%')), 2)
    except ValueError:
        return None


def vals(s):
    out = []
    for m in re.finditer(r'[-−(]?\d[\d,]*(?:\.\d+)?', s):
        v = canon(m.group(0))
        if v is None:
            continue
        tail = s[m.end():m.end() + 6].strip()
        um = re.match(r'(亿元|万元|千元|元)', tail[:3]) if tail else None
        out.append((v, um.group(1) if um else '元'))
    return out


def match(val, s, fu=None):
    v = canon(val)
    if v is None:
        return False
    for sv, su in vals(s):
        for base in ('元', fu or '元'):
            if abs(v * FACT.get(base, 1) / FACT.get(su, 1) - sv) <= max(abs(sv) * 1e-6, 0.02):
                return True
        if abs(v - sv) <= max(abs(sv) * 1e-9, 1e-6):
            return True
    return False


rows = [json.loads(l) for l in (D / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
print(f'池内候选 {len(rows)}')
print()
stat = Counter()
pure = []
for r in rows:
    s = (r.get('claim_text') or '').strip()
    if len(s) < 12 or not r.get('metric'):
        continue
    if r['metric'] not in s:
        continue
    if not match(r.get('value'), s, r.get('unit')):
        continue
    if GROWTH.search(s):
        stat['含增长标志（不能作单值题）'] += 1
    else:
        stat['纯数值句（可作单值题）'] += 1
        pure.append(r)

for k, v in stat.most_common():
    print(f'  {k:26} {v}')
print()
print(f'=== 纯单值锚点的公司分布 ===')
byco = Counter(r['company'] for r in pure)
print(f'  公司数 {len(byco)} · 每题若出 1 条，可做 {len(pure)} 题')
print(f'  分布（前 12）: {byco.most_common(12)}')
print()
print('=== 样例 ===')
for r in pure[:6]:
    print(f"  {r['company']} {r['metric'][:24]:24} 值={r['value']}")
    print(f"      「{(r.get('claim_text') or '')[:88]}」")
print()
print('=== 结论 ===')
print(f'  v3 单值题的真实上限约 {len(pure)} 题（来自 {len(byco)} 家公司）。')
print(f'  若与增长题 1:1 配平，总题量上限约 {len(pure)*2} 题。')
