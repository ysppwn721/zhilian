"""量化训练语料的上期偏差：正文里到底有多少"上期引用"可用。

背景：build_candidates_v2.py 的锚点匹配是
    if f['current'] not in nums: continue        # 只认本期值
    if GROWTH_WORD.search(s): growth_hit = ...   # 增长率类也锚在本期值上
且输出里 period 硬编码为 '本期'。因此:
  - 所有候选的「正确答案」都是本期事实
  - "总选本期" 在训练集上是 100%（已实测）
  - 候选的 period 字段直接泄漏答案

本脚本度量：若改为也锚定「上期值」，能多出多少样本。
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
D = ROOT / '答辩评测' / 'annual_reports_final'
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
PRI_WORD = re.compile(r'上期|上年同期|去年同期|上年度|期初')
GROWTH = re.compile(r'同比|较上年|较上期|增减|变动|增长|下降|上升|减少|增加|增幅')


def canon(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').replace('－', '-').strip('()')
    try:
        return round(float(x.rstrip('%')), 2)
    except ValueError:
        return None


def main() -> int:
    import pymupdf
    import pdfplumber

    mf = [json.loads(l) for l in (D / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    tc = [json.loads(l) for l in (D / 'training_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]

    print('=== 1) 现有训练集的 period 分布 ===')
    print(f'  {dict(Counter(r.get("period") for r in tc))}')
    print()

    print('=== 2) 现有候选的锚点是否只认本期值 ===')
    # 逐条检查 claim_text 里是否含本期值 / 上期值
    only_cur = only_pri = both = neither = 0
    for r in tc:
        s = r.get('claim_text') or ''
        nums = {canon(m.group(0)) for m in NUM.finditer(s)}
        nums.discard(None)
        c, p = r.get('value'), r.get('fact_prior_value')
        hc, hp = c in nums, p in nums
        if hc and hp:
            both += 1
        elif hc:
            only_cur += 1
        elif hp:
            only_pri += 1
        else:
            neither += 1
    n = len(tc)
    print(f'  claim 文本含本期值: {only_cur} (仅本期) + {both} (两者都有)')
    print(f'  claim 文本含上期值: {only_pri} (仅上期) + {both}')
    print(f'  都不含（应为 0）  : {neither}')
    print()

    print('=== 3) 上期偏差的直接后果 ===')
    print(f'  训练集「总选本期+合并」= 100%（已实测），因为正例恒为本期事实。')
    print()

    print('=== 4) 若同时锚定「上期值」，可新增多少样本 ===')
    # 抽样 60 份，统计"正文句含上期值 + 指标名 + 上期词"的可用锚点
    sample = mf[:60]
    extra = Counter()
    for rec in sample:
        p = D / rec['local_file']
        if not p.is_file():
            continue
        try:
            doc = pymupdf.open(p)
            pages = [(i + 1, (pg.get_text('text') or '')) for i, pg in enumerate(doc)]
            doc.close()
        except Exception:
            continue
        # 取该公司的现有候选，拿到 (metric, prior) 对
        mine = [r for r in tc if r['company'] == rec['stock_code']]
        if not mine:
            continue
        sents = []
        for pno, txt in pages:
            for s in re.split(r'[。！？；;\n]', txt):
                s = re.sub(r'\s+', ' ', s).strip()
                if 8 <= len(s) <= 240:
                    sents.append((pno, s))
        for r in mine:
            metric = r['metric']
            pri = r.get('fact_prior_value')
            if pri is None:
                continue
            hit = None
            for pno, s in sents:
                if metric[:6] not in s:
                    continue
                if canon_of := {canon(m.group(0)) for m in NUM.finditer(s)}:
                    if pri in canon_of and PRI_WORD.search(s):
                        hit = (pno, s)
                        break
            extra['prior_anchored' if hit else 'no_prior_anchor'] += 1
    print(f'  抽样 {len(sample)} 份，覆盖现有候选 {sum(extra.values())} 条')
    print(f'    正文中提到**上期值**且带期间词的: {extra["prior_anchored"]}')
    print(f'    正文中未提上期值              : {extra["no_prior_anchor"]}')
    tot = sum(extra.values())
    if tot:
        print(f'    → 若改为双锚点，理论上可把 {extra["prior_anchored"]/tot*100:.1f}% 的样本'
              f'改造成「正确答案是上期」')
    print()
    print('=== 结论 ===')
    print('  训练集的"正确答案恒为本期"，与 holdout 的 (current,) / (current,prior) 同构，')
    print('  所以两者都奖励"本期"这一条零信息规则。这不是模型问题，是语料构造问题。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
