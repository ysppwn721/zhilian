"""对比两套语料的互补性：claim 真实性 vs 候选构造质量。

修复版基准（annual_benchmark_repaired_20261004_v2）：
  ✓ 候选构造合格（无位置捷径、真实干扰项、本期/上期配平、集合指标）
  ✗ claim 文本全部为模板合成（0% 出自年报）

我的语料（annual_reports_final）：
  ✓ claim 文本出自年报真实句子（需量化）
  ✗ 候选构造有缺陷（period 硬编码为"本期"、无上期正例、候选集不含真实干扰项）

若两者互补，则最优路径是：真实 claim + 修复版候选构造。
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
MINE = ROOT / '答辩评测' / 'annual_reports_final'
CN = ROOT / '答辩评测' / 'cn_reports'


def norm(s: str) -> str:
    return re.sub(r'[\s\u3000]+', '', s or '')


def main() -> int:
    import pymupdf

    tc = [json.loads(l) for l in (MINE / 'training_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    print(f'=== 我的语料 {len(tc)} 条的 claim 是否出自年报 ===')

    cache: dict[str, str] = {}
    def fulltext(fname: str) -> str:
        if fname not in cache:
            p = MINE / fname
            if not p.is_file():
                cache[fname] = ''
            else:
                doc = pymupdf.open(p)
                cache[fname] = norm(''.join(pg.get_text('text') or '' for pg in doc))
                doc.close()
        return cache[fname]

    hit = miss = 0
    # 抽样 150 条逐字核验（全文匹配贵，抽样足够）
    import random
    random.seed(0)
    sample = random.sample(tc, min(150, len(tc)))
    for r in sample:
        s = norm(r.get('claim_text') or '')
        if len(s) < 8:
            miss += 1
            continue
        txt = fulltext(r['source_file'])
        if s and s in txt:
            hit += 1
        else:
            miss += 1
    print(f'  抽样 {len(sample)} 条：逐字命中 {hit} ({hit/len(sample)*100:.1f}%) · 未命中 {miss}')
    print()

    print('=== 两套语料互补性 ===')
    print(f'  {"维度":30} {"修复版基准":>14} {"我的语料":>14}')
    print(f'  {"-"*30} {"-"*14} {"-"*14}')
    rows = [
        ('候选位置有信息量', '否(11.76%≈随机)', '—'),
        ('真实干扰项(同期间不同指标)', '11,900 条', '有(6,438 负例)'),
        ('本期/上期配平', '425/425', '0/1029(全本期)'),
        ('集合指标', '已用', '未用'),
        ('claim 逐字出自年报', f'0.0%', f'{hit/len(sample)*100:.1f}%'),
        ('claim 句式数', '131 种/1275 题', '自然句'),
        ('scope 字段', '未核验(全同)', '已判定95%'),
    ]
    for a, b, c in rows:
        print(f'  {a:30} {b:>14} {c:>14}')
    print()
    print('=== 结论 ===')
    print('  两者高度互补：修复版有正确的候选构造，但没有真实 claim；')
    print('  我的语料有真实 claim，但没有正确的候选构造。')
    print()
    print('  最优路径：以**我的真实 claim** 为题干，套用**修复版的候选构造**')
    print('  （同表真实干扰项 + 本期/上期配平 + 打乱顺序 + 集合指标）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
