"""口径判定器：分三级证据，逐条给出判定与依据；判不了的标 unresolvable。

三级证据（可靠性递减，但都可审计）
--------------------------------
T1 显式证据
    - caption/表内首行出现「母公司」→ 母公司
    - caption/页面按行业/产品/地区/板块拆分 → 分部
    - 年报格式准则：第二节「主要会计数据/主要财务指标」→ 合并

T2 数值交叉验证（最强的一类，因为它不依赖文字）
    同一指标若在「主要会计数据」表（T1 已定为合并）里出现且数值相同，
    则本表这一行也是合并口径。

T3 表头形态 + 章节惯例（默认值，可审计）
    - MD&A「科目变动分析表」形态（科目|本期数|上年同期数|变动比例）且未标母公司 → 合并
    - 「资产及负债状况」形态（项目名称|本期期末数|占总资产比例|上期期末数）→ 合并

判不了的不猜，标 unresolvable，列出去重后的少量单元供人工处理。

输出：scope_decisions.jsonl（逐条）+ scope_decisions_summary.json
"""
from __future__ import annotations

import argparse
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
SCOPE_WORD = re.compile(r'母公司|公司本部|合并|分部|分产品|分行业|分地区|业务板块')
CONSOLIDATED_TITLE = re.compile(r'主要会计数据|主要财务指标|前三年主要会计数据')
SEGMENT_TITLE = re.compile(r'分行业|分产品|分地区|业务板块|区域及港口|主营业务分')
# T3 表头形态
MDNA_HEADER = re.compile(r'本期数|上年同期数|变动比例|同比增减|变动原因')
BS_HEADER = re.compile(r'本期期末数|占总资产的比例|上期期末数|期末余额|期初余额|'
                       r'期末数|期初数|当期变动|期末账面')
# 准则第二十一条列举的董事会报告项目（费用/研发/现金流变动分析）——同为合并口径
SEC21_TITLE = re.compile(r'销售费用|管理费用|财务费用|研发投入|研发费用|现金流量|'
                         r'非经常性损益|营业收入扣除|利润表及现金流量表')
# 章节标题（用于判断所在章节）
SEC2 = re.compile(r'第二节|公司简介和主要财务指标|主要会计数据')

# 判定依据出处（写入每条判定结果，便于复核与答辩引用）
BASIS = {
    'rule19_3': '《公开发行证券的公司信息披露内容与格式准则第2号——年度报告的内容与格式》'
                '第十九条（三）：编制合并财务报表的公司应当以合并财务报表数据填列或计算'
                '主要会计数据和财务指标。',
    'rule21_1': '同上第二十一条（一）：董事会报告应列示营业收入、成本、费用、研发投入、'
                '现金流等项目的同比变动情况及原因（合并口径讨论分析）。',
    'rule60': '同上第六十条：编制合并财务报表的公司，除提供合并财务报表外，还应当提供'
              '母公司财务报表——母公司口径必另行列表，故未标"母公司"的即为合并数。',
}


def clean(c):
    return re.sub(r'\s+', '', str(c or ''))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    args = ap.parse_args()
    d = args.corpus

    import pymupdf
    import pdfplumber

    cs = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]

    # 先建「主要会计数据表(合并)」的数值索引，供 T2 交叉验证
    consolidated_values: dict[tuple[str, str], set] = defaultdict(set)
    for c in usable:
        if CONSOLIDATED_TITLE.search(c.get('caption') or ''):
            if c['value'] is not None:
                consolidated_values[(c['company'], c['metric'])].add(c['value'])
            if c['fact_prior_value'] is not None:
                consolidated_values[(c['company'], c['metric'])].add(c['fact_prior_value'])

    cache: dict[tuple, dict] = {}

    def page_info(company, fname, pno, metric=''):
        key = (company, pno)
        if key in cache:
            return cache[key]
        info = {'rows': [], 'scope_sents': [], 'in_sec2': False, 'unit_line': '',
                'table_matched': False}
        p = d / fname
        if p.is_file():
            doc = pymupdf.open(p)
            try:
                if 1 <= pno <= len(doc):
                    text = doc[pno - 1].get_text('text') or ''
                    info['in_sec2'] = bool(SEC2.search(text[:600]))
                    m = re.search(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)', text)
                    if m:
                        info['unit_line'] = m.group(0)
                    for s in re.split(r'[。\n]', text):
                        s = s.strip()
                        if s and SCOPE_WORD.search(s) and len(s) <= 80:
                            info['scope_sents'].append(s)
            finally:
                doc.close()
            with pdfplumber.open(p) as pdf:
                if 1 <= pno <= len(pdf.pages):
                    tabs = pdf.pages[pno - 1].extract_tables() or []
                    # 取**包含该指标的**那张表，而不是页面上最大的表。
                    # 旧做法（取最大表）会把别的表的表头安到这条事实上——
                    # 实测出现「销售费用」配「公司研发人员的数量 151」这种错配。
                    target = None
                    if metric:
                        for t in tabs:
                            flat = ''.join(clean(c) for row in t[:6] for c in row)
                            if clean(metric)[:8] and clean(metric)[:8] in flat:
                                target = t
                                info['table_matched'] = True
                                break
                    if target is None:
                        target = max(tabs, key=lambda x: len(x) * max((len(r) for r in x), default=0)) if tabs else None
                    if target is not None:
                        info['rows'] = [[clean(c)[:26] for c in row[:6]] for row in target[:4]]
        cache[key] = info
        return info

    out = []
    for c in usable:
        caption = c.get('caption') or ''
        info = page_info(c['company'], c['source_file'], c['fact_page'] or 1, c['metric'] or '')
        head = ' '.join(info['rows'][0]) if info['rows'] else ''
        sents = ' '.join(info['scope_sents'])

        # ---- 一致性校验：该指标是否真出现在这张表里 ----
        # caption 是**表格标题**，metric 是**表内某一行**，二者本就不该相等，
        # 所以不能拿 caption 去比 metric（那样会误杀 96%）。正确判据是：
        # 取表头的那张表里，是否真的含有这个指标行。
        coherent = bool(info['rows']) and (not c['metric'] or info['table_matched'])
        verdict = why = tier = None
        if not coherent:
            why = ('该指标未出现在取到表头的表格中——表头可能来自页面上另一张表，'
                   '此记录的证据链不完整')
            tier = 'X'

        # ---- T1 显式证据 ----
        if not coherent:
            pass                      # 已置 tier='X'，不参与判定
        elif re.search(r'母公司|公司本部', caption):
            verdict, why, tier = '母公司', 'caption 含「母公司」', 'T1'
        elif info['rows'] and re.search(r'母公司|公司本部', ' '.join(info['rows'][0])):
            verdict, why, tier = '母公司', '表内首行含「母公司」', 'T1'
        elif re.search(r'母公司|公司本部', sents) and not re.search(r'合并', sents):
            verdict, why, tier = '母公司', '页面出现「母公司」且无「合并」', 'T1'
        elif SEGMENT_TITLE.search(caption) or re.search(r'分行业|分产品|分地区', sents):
            verdict, why, tier = '分部', '标题或页面按行业/产品/地区/板块拆分', 'T1'
        elif CONSOLIDATED_TITLE.search(caption):
            verdict, why, tier = '合并', '年报第三节会计数据和财务指标摘要', 'T1'
        # ---- T2 数值交叉验证 ----
        elif (c['company'], c['metric']) in consolidated_values and (
                c['value'] in consolidated_values[(c['company'], c['metric'])] or
                c['fact_prior_value'] in consolidated_values[(c['company'], c['metric'])]):
            verdict, why, tier = '合并', '该指标数值与本公司「主要会计数据」表（合并）一致', 'T2'
        # ---- T3 表头形态 + 准则依据 ----
        elif MDNA_HEADER.search(head) and not re.search(r'母公司', head):
            verdict, why, tier = '合并', '董事会报告变动分析表形态，未标母公司', 'T3'
        elif BS_HEADER.search(head) and not re.search(r'母公司', head):
            verdict, why, tier = '合并', '资产负债状况表形态（占总资产比例），未标母公司', 'T3'
        elif SEC21_TITLE.search(caption) and not re.search(r'母公司', caption):
            verdict, why, tier = '合并', '准则第二十一条列举的董事会报告项目，未标母公司', 'T3'
        # 三年「主要会计数据」表的结构判据：
        #   表头 = 年份 | 年份 | 本年比上年增减 | 年份（或类似）
        # 这类 caption 常抽错（抽到勾选框问句「公司是否需追溯调整或重述以前年度会计数据」
        # 或单位行「单位：元」），但表结构本身足以识别。
        elif (len(re.findall(r'20\d{2}', head)) >= 2
              and re.search(r'增减|变动|变化率', head)
              and not re.search(r'母公司', head)):
            verdict, why, tier = '合并', '三年主要会计数据/财务指标表结构，未标母公司', 'T3'

        out.append({
            'company': c['company'],
            'source_file': c['source_file'],
            'metric': c['metric'],
            'claim_kind': c['claim_kind'],
            'value': c['value'],
            'fact_page': c['fact_page'],
            'claim_page': c['claim_page'],
            'unit': c['unit'],
            'caption': caption,
            'caption_coherent': coherent,
            'scope_verdict': verdict,
            'scope_evidence_tier': tier,
            'scope_reason': why,
            'scope_basis': ('rule19_3' if tier == 'T1' and verdict == '合并' else
                            'rule21_1' if tier == 'T3' else
                            'rule60' if verdict == '母公司' else
                            'numeric_crosscheck' if tier == 'T2' else None),
            'table_head': head,
            'scope_sentences': info['scope_sents'][:2],
            'three_value_check': c['three_value_check'],
            'label': None,
        })

    (d / 'scope_decisions.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in out), encoding='utf-8')

    decided = [r for r in out if r['scope_verdict']]
    undecided = [r for r in out if not r['scope_verdict']]
    tiers = Counter(r['scope_evidence_tier'] for r in decided)
    summary = {
        'total': len(out),
        'decided': len(decided),
        'undecided': len(undecided),
        'tier_counts': dict(tiers),
        'verdict_counts': dict(Counter(r['scope_verdict'] for r in decided)),
        'undecided_by_caption': dict(Counter((r['caption'] or '（空）')[:44] for r in undecided).most_common()),
        'note': '自动判定；T3 为章节惯例默认值，建议抽样复核。未判定项不进入训练集。',
    }
    (d / 'scope_decisions_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'候选 {len(out)} 条')
    print(f'  已判定 {len(decided)} 条 ({len(decided)/len(out)*100:.0f}%)  '
          f'→ {dict(Counter(r["scope_verdict"] for r in decided))}')
    for t in ('T1', 'T2', 'T3'):
        print(f'      {t}: {tiers.get(t,0):>4} 条')
    print(f'  未判定 {len(undecided)} 条 ({len(undecided)/len(out)*100:.0f}%)')
    print()
    print('  未判定的 caption 分布:')
    for k, v in Counter((r['caption'] or '（空）')[:44] for r in undecided).most_common(10):
        print(f'    {v:>4}  「{k}」')
    print()
    print('  未判定样例（这些必须人工看）:')
    for r in undecided[:5]:
        print(f"    {r['company']} 「{r['caption'][:34]}」 表页={r['fact_page']}")
        print(f"        表头: {r['table_head'][:86]}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
