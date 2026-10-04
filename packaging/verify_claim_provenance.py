"""决定性检验：benchmark 的 claim_text 是否出自年报正文。

若是真实引用，claim 文本（或其数值+指标部分）应能在源 PDF 文本层中找到。
若是模板合成，则找不到，且会呈现固定句式。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / '答辩评测' / 'annual_benchmark_repaired_20261004_v2'
CN = ROOT / '答辩评测' / 'cn_reports'


def norm(s: str) -> str:
    return re.sub(r'[\s\u3000]+', '', s or '')


def main() -> int:
    import pymupdf

    rows = [json.loads(l) for l in (D / 'benchmark.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    byq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byq[r['claim_id']].append(r)
    qs = list(byq.values())

    # 缓存源 PDF 全文
    cache: dict[str, str] = {}
    def fulltext(fname: str) -> str:
        if fname not in cache:
            p = CN / fname
            if not p.is_file():
                cache[fname] = ''
            else:
                doc = pymupdf.open(p)
                cache[fname] = norm(''.join(pg.get_text('text') or '' for pg in doc))
                doc.close()
        return cache[fname]

    print('=== 1) claim 文本是否逐字出现在源年报中 ===')
    verbatim = partial = absent = 0
    samples = {'partial': [], 'absent': []}
    for q in qs:
        lead = q[0]
        claim = norm(lead['claim_text'])
        src = lead.get('source_file') or ''
        txt = fulltext(src)
        if not txt:
            absent += 1
            continue
        if claim and claim in txt:
            verbatim += 1
            continue
        # 去掉前缀主体与年份，试核心片段
        core = re.sub(r'^上市公司\d+', '', claim)
        core = re.sub(r'^20\d{2}年', '', core)
        core = re.sub(r'同比变化[-+]?[\d.]+%$', '', core)
        if core and len(core) >= 8 and core in txt:
            partial += 1
            if len(samples['partial']) < 4:
                samples['partial'].append((lead, core))
        else:
            absent += 1
            if len(samples['absent']) < 4:
                samples['absent'].append(lead)

    n = len(qs)
    print(f'  逐字命中          : {verbatim:>5} / {n}  ({verbatim/n*100:.1f}%)')
    print(f'  去前缀后命中      : {partial:>5} / {n}  ({partial/n*100:.1f}%)')
    print(f'  完全找不到        : {absent:>5} / {n}  ({absent/n*100:.1f}%)')
    print()

    print('=== 2) claim 句式统计（模板化会高度集中）===')
    pat = Counter()
    for q in qs:
        c = q[0]['claim_text']
        p = re.sub(r'\d', '#', c)
        p = re.sub(r'#+', '#', p)
        pat[p] += 1
    print(f'  不同句式数: {len(pat)} / {n} 题')
    for k, v in pat.most_common(6):
        print(f'    {v:>5}  {k[:78]}')
    print()

    print('=== 3) 样例 ===')
    for lead, core in samples['partial']:
        print(f'  [去前缀命中] 「{lead["claim_text"][:70]}」')
    for lead in samples['absent']:
        print(f'  [找不到]    「{lead["claim_text"][:70]}」  源={lead.get("source_file")}')
    print()

    print('=== 4) 结论 ===')
    top = pat.most_common(1)[0][1] if pat else 0
    if verbatim / n < 0.05:
        print(f'  ✗ 仅 {verbatim/n*100:.1f}% 的 claim 逐字出现在年报中，且前 {min(6,len(pat))} 种句式')
        print(f'    覆盖 {sum(v for _, v in pat.most_common(6))/n*100:.1f}% 的题目 —— claim 文本由模板合成，')
        print(f'    不是年报原文引用。该基准测的是"合成句式上的字段匹配"，')
        print(f'    不能代表真实年报文本上的关联能力。')
    else:
        print(f'  ✓ {verbatim/n*100:.1f}% 逐字命中，claim 来自原文。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
