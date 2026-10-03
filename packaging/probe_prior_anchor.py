"""验证语料能否构造「上期锚点」样本 —— 这是要求文档点名的硬约束。

背景：旧试训数据被审计拦下的原因之一是 `always_current_top1 = 100%`。
根因是构造器只产生了「正文引用本期值 + 候选=本期/上期/错误期间」这一种形状，
于是"永远选本期"就是满分。

要求文档明确要求：「同时需要本期引用和真实上期引用，不能所有样本都正标本期」。
本脚本实测：这 50 份语料里，能构造出多少条**以上期为正确来源**的样本。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / '答辩评测' / 'annual_reports_new_20261002'
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
METRIC_HINTS = ('收入', '成本', '费用', '利润', '资产', '负债', '现金', '合计', '总额', '余额', '每股')
CUR_MARK = re.compile(r'本期|本报告期|期末|本年度|报告期')
PRI_MARK = re.compile(r'上期|上年同期|上年|期初|上年度|去年同期')
GROWTH_MARK = re.compile(r'同比|环比|增减|增长|下降|上升|变动|增幅')


def canon(x):
    x = x.replace(',', '').replace('−', '-').replace('(', '-').replace(')', '')
    try:
        return round(float(x.rstrip('%')), 2)
    except ValueError:
        return None


def sentences(text):
    for s in re.split(r'[。！？；;\n]', text):
        s = re.sub(r'\s+', ' ', s).strip()
        if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
            yield s


def main() -> int:
    import pymupdf
    import pdfplumber

    rows = [json.loads(l) for l in (CORPUS / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    stats = Counter()
    samples = {'prior_anchor': [], 'current_anchor': [], 'growth': []}

    for r in rows:
        pdf = CORPUS / r['local_file']
        if not pdf.is_file():
            continue
        doc = pymupdf.open(pdf)
        text = ''.join((p.get_text('text') or '') for p in doc)
        doc.close()
        sents = list(sentences(text))

        # 抽出 (指标, 本期值, 上期值) 三值组
        triples = []
        with pdfplumber.open(pdf) as pd:
            for page in pd.pages:
                for t in (page.extract_tables() or []):
                    head = ' '.join(str(c or '') for row in t[:3] for c in row)
                    if not (CUR_MARK.search(head) and PRI_MARK.search(head)):
                        continue
                    for row in t:
                        cells = [re.sub(r'\s+', '', str(c or '')) for c in row]
                        if not cells or not cells[0] or len(cells[0]) > 30:
                            continue
                        if not any(k in cells[0] for k in METRIC_HINTS):
                            continue
                        vals = [canon(c) for c in cells[1:]]
                        vals = [v for v in vals if v is not None]
                        if len(vals) >= 2:
                            triples.append((cells[0], vals[0], vals[1]))

        for metric, cur, prior in triples:
            stats['triples'] += 1
            # 找引用本期值的句子
            cur_sents = [s for s in sents if metric in s and any(canon(m.group(0)) == cur for m in NUM.finditer(s))]
            # 找引用上期值的句子
            pri_sents = [s for s in sents if metric in s and any(canon(m.group(0)) == prior for m in NUM.finditer(s))]
            if cur_sents:
                stats['current_anchor'] += 1
                if len(samples['current_anchor']) < 3:
                    samples['current_anchor'].append((r['stock_code'], metric, cur, cur_sents[0][:90]))
            if pri_sents:
                stats['prior_anchor'] += 1
                if len(samples['prior_anchor']) < 3:
                    samples['prior_anchor'].append((r['stock_code'], metric, prior, pri_sents[0][:90]))
            # 增长率类：同一句里同时出现两个值和同比词
            growth = [s for s in sents if metric in s and GROWTH_MARK.search(s)
                      and any(canon(m.group(0)) == cur for m in NUM.finditer(s))]
            if growth:
                stats['growth'] += 1
                if len(samples['growth']) < 3:
                    samples['growth'].append((r['stock_code'], metric, cur, growth[0][:90]))

    print('=== 「本期 / 上期」两种锚点各能构造多少 ===')
    t = max(1, stats['triples'])
    for k, label in (('triples', '三值组总数'),
                     ('current_anchor', '本期值有正文锚点'),
                     ('prior_anchor', '上期值有正文锚点'),
                     ('growth', '增长率类句子')):
        print(f'  {label:18} {stats[k]:>6}  ({stats[k]/t*100:5.1f}% of 三值组)')

    print()
    ratio = stats['prior_anchor'] / max(1, stats['current_anchor'])
    print(f'  上期 / 本期 锚点比 = {ratio:.2f}')
    if ratio >= 0.3:
        print('  ✓ 上期锚点充足，可以构造「正确答案是上期」的样本，')
        print('    从而打破「永远选本期」的捷径（旧数据此处为 0）。')
    else:
        print('  ⚠ 上期锚点偏少，构造样本时需注意配平，否则捷径仍可能存在。')

    print('\n=== 样例 ===')
    for key, title in (('current_anchor', '本期锚点'), ('prior_anchor', '上期锚点'), ('growth', '增长率')):
        print(f'  -- {title} --')
        for code, metric, val, sent in samples[key]:
            print(f'     {code} {metric} = {val}')
            print(f'        「{sent}」')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
