"""验证「本期绝对值 + 增长率」句式能否拆成两条训练样本。

关键洞察
--------
中文年报正文的典型句式是：
    报告期内，公司实现营业收入 1,766,447,155.08 元，较上年同期增加 1.85%

它同时给出 **本期绝对值** 与 **增长率**，但**不给上期绝对值**。
上期绝对值在表的第二列里。

因此从这一句可以拆出两类样本：
  (a) 本期引用类：claim=该句 → 正例=本期事实；负例=上期事实、错指标、错主体
  (b) 增长率类  ：claim=该句 → 正例={本期事实, 上期事实}（来源集合，两个都对）
     并按要求用「集合完整率 + 集合精确匹配」评价，而不是 Top-1

这样上期事实不再是"正文锚点缺失"，而是**增长率类的必要来源之一**，
本期/上期比例可以接近 1:1 —— 不是靠硬凑，而是靠把句式拆对。
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
UNIT_PAT = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')
METRIC_HINTS = ('营业收入', '营业成本', '净利润', '利润总额', '销售费用', '管理费用',
                '研发费用', '财务费用', '资产总额', '资产总计', '负债总额', '净资产',
                '经营活动产生的现金流量净额', '现金流量净额', '每股收益', '毛利率')
# 句子里的金额（≥5 位数字，排除百分比与年份）
CUR_PAT = re.compile(r'本期|本报告期|本期数|本期金额|期末|期末数|本年度|报告期')
PRI_PAT = re.compile(r'上期|上年同期|上期数|上期金额|期初|期初数|上年度|去年同期')
CHG_PAT = re.compile(r'增减|变动|变动比例|同比|增幅')
YEAR_PAT = re.compile(r'^(20\d{2})\s*年?$')
NARRATIVE = re.compile(r'实现|达到|完成|同比|较上年|较上期|增长|下降|提升|减少|增加')
SEC2_PAT = re.compile(r'第\s*二\s*节\s*公司简介和主要财务指标|主要会计数据和财务指标')
SEC3_PAT = re.compile(r'第\s*三\s*节\s*(?:管理层讨论与分析|经营情况讨论与分析)|管理层讨论与分析|经营情况讨论与分析')
TOC_PAT = re.compile(r'目\s*录')


def canon(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').replace('－', '-')
    neg = x.startswith('(') and x.endswith(')')
    x = x.strip('()')
    try:
        v = round(float(x.rstrip('%')), 2)
    except ValueError:
        return None
    return -v if neg else v


def clean(c):
    return re.sub(r'\s+', '', str(c or ''))


def cell_kind(c):
    c = c.strip()
    if not c:
        return 'empty'
    if c.endswith('%'):
        return 'pct'
    return 'num' if re.match(r'^-?[\d,]+(?:\.\d+)?$', c) else 'text'


def header_rows(t, max_scan=4):
    out = []
    for i, row in enumerate(t[:max_scan]):
        cells = [clean(c) for c in row]
        ks = [cell_kind(c) for c in cells]
        if sum(1 for k in ks if k != 'empty') >= 3 and sum(1 for k in ks if k in ('num', 'pct')) >= 2 \
                and sum(1 for k in ks if k == 'text') >= 1 and i > 0:
            break
        out.append(cells)
    return out or [[clean(c) for c in t[0]]]


def classify(hr, ncols):
    col = [' '.join(r[c] for r in hr if c < len(r) and r[c]) for c in range(ncols)]
    roles = ['unknown'] * ncols
    if col and len(col[0]) > 2:
        roles[0] = 'metric'
    ec = [c for c, t in enumerate(col) if c and CUR_PAT.search(t) and not CHG_PAT.search(t)]
    ep = [c for c, t in enumerate(col) if c and PRI_PAT.search(t) and not CHG_PAT.search(t)]
    yr = {c: int(YEAR_PAT.match(t.strip()).group(1)) for c, t in enumerate(col)
          if c and YEAR_PAT.match(t.strip())}
    if ec and ep:
        roles[ec[0]] = 'current'; roles[ep[0]] = 'prior'
    elif len(yr) >= 2:
        o = sorted(yr.items(), key=lambda kv: -kv[1])
        roles[o[0][0]] = 'current'; roles[o[1][0]] = 'prior'
    for c, t in enumerate(col):
        if c and CHG_PAT.search(t) and roles[c] == 'unknown':
            roles[c] = 'change'
    return roles, col


def locate(pages):
    s2, s3 = set(), set()
    n = len(pages)
    for idx, (pno, txt) in enumerate(pages):
        head = txt[:600]
        if TOC_PAT.search(head) and idx < 8:
            continue
        if SEC2_PAT.search(head):
            s2.update(range(pno, min(n, pno + 4) + 1))
        if SEC3_PAT.search(head):
            s3.update(range(pno, min(n, pno + 30) + 1))
    return s2, s3


def metric_match_level(metric: str, sentence: str) -> str:
    """返回匹配强度：'exact'（表格指标名原样出现在句中）/ 'core'（去修饰后命中）/ 'fragment'（片段命中）/ ''（未命中）。

    之所以要分级：放宽匹配提高了产量，但可能引入噪声（例如「净利润」命中了
    「归属于上市公司股东的净利润」以外的另一条利润口径）。分级后可以报告
    "其中多少是精确命中"，把放宽的代价量化出来，而不是假装没有代价。
    """
    if not metric or not sentence:
        return ''
    if metric in sentence:
        return 'exact'
    core = metric
    for noise in ('（元）', '(元)', '（%）', '(%)', '变动原因说明', '总额', '净额',
                  '归属于上市公司股东的', '归属于母公司股东的', '产生的', '其中：'):
        core = core.replace(noise, '')
    core = core.strip('：: ')
    if core and core in sentence:
        return 'core'
    for seg in re.findall(r'[\u4e00-\u9fff]{3,}', core):
        if seg in sentence:
            return 'fragment'
    return ''


def metric_matches(metric: str, sentence: str) -> bool:
    """指标名与正文用词是否算同一处引用。分级判定见 metric_match_level。"""
    return bool(metric_match_level(metric, sentence))


def main() -> int:
    import pymupdf
    import pdfplumber

    rows = [json.loads(l) for l in (CORPUS / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    st = Counter()
    # (company, metric, 本期值, 上期值, claim 句, 页)
    current_pairs, growth_pairs = [], []
    multi_fact_claims = []

    for rec in rows:
        pdf = CORPUS / rec['local_file']
        if not pdf.is_file():
            continue
        doc = pymupdf.open(pdf)
        pages = [(i + 1, (p.get_text('text') or '')) for i, p in enumerate(doc)]
        doc.close()
        s2, s3 = locate(pages)
        target = s2 | s3
        st['reports'] += 1
        st['pages_target'] += len(target)

        sents = []
        for pno, txt in pages:
            if pno not in target:
                continue
            for s in re.split(r'[。！？；;\n]', txt):
                s = re.sub(r'\s+', ' ', s).strip()
                if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
                    sents.append((pno, s))

        with pdfplumber.open(pdf) as pdf:
            for pno in sorted(target):
                if pno < 1 or pno > len(pdf.pages):
                    continue
                page = pdf.pages[pno - 1]
                ptext = pages[pno - 1][1]
                um = UNIT_PAT.search(ptext)
                unit = um.group(2) if um else None
                for t in (page.extract_tables() or []):
                    if not t or len(t) < 2:
                        continue
                    hr = header_rows(t)
                    flat = ' '.join(c for r in hr for c in r)
                    if not (CUR_PAT.search(flat) or len(re.findall(r'20\d{2}', flat)) >= 2):
                        continue
                    if not (PRI_PAT.search(flat) or len(re.findall(r'20\d{2}', flat)) >= 2):
                        continue
                    roles, col = classify(hr, max(len(r) for r in t))
                    cc = next((c for c, r in enumerate(roles) if r == 'current'), None)
                    pc = next((c for c, r in enumerate(roles) if r == 'prior'), None)
                    if cc is None or pc is None:
                        continue
                    for row in t:
                        cells = [clean(c) for c in row]
                        if not cells or not cells[0] or len(cells[0]) > 30:
                            continue
                        metric = cells[0]
                        if len(metric) < 2:
                            continue
                        # 指标筛选放宽：只要不是纯符号/表头噪声即可进入候选，
                        # 由后续「正文锚点 + 三值校验」把关（旧版这里用 16 词白名单，过窄）。
                        if re.fullmatch(r'[\d\s,.%()（）\-—/]+', metric):
                            continue
                        if any(k in metric for k in ('合计', '小计', '其中', '附注', '说明', '序号')):
                            continue
                        cur = canon(cells[cc]) if cc < len(cells) else None
                        pri = canon(cells[pc]) if pc < len(cells) else None
                        if cur is None or pri is None:
                            continue
                        st['facts'] += 1
                        # 找同时含本期值 + 增长词的正文句（记录匹配强度）
                        hit = None
                        level = ''
                        for sp, s in sents:
                            lv = metric_match_level(metric, s)
                            if not lv:
                                continue
                            nums = [canon(m.group(0)) for m in NUM.finditer(s)]
                            if cur not in nums:
                                continue
                            if not re.search(r'同比|较上年|较上期|增减|变动|增长|下降', s):
                                continue
                            hit, level = (sp, s), lv
                            break
                        if hit:
                            st['growth_claims'] += 1
                            st[f'growth_level_{level}'] += 1
                            growth_pairs.append({
                                'company': rec['stock_code'], 'metric': metric,
                                'current': cur, 'prior': pri, 'unit': unit,
                                'claim_page': hit[0], 'claim_text': hit[1], 'match_level': level,
                            })
                        # 只含本期绝对值、无增长词 → 本期引用类
                        else:
                            hit2 = None
                            level2 = ''
                            for sp, s in sents:
                                lv = metric_match_level(metric, s)
                                if not lv:
                                    continue
                                nums = [canon(m.group(0)) for m in NUM.finditer(s)]
                                if cur in nums:
                                    hit2, level2 = (sp, s), lv
                                    break
                            if hit2:
                                st['quote_claims'] += 1
                                current_pairs.append({
                                    'company': rec['stock_code'], 'metric': metric,
                                    'current': cur, 'prior': pri, 'unit': unit,
                                    'claim_page': hit2[0], 'claim_text': hit2[1], 'match_level': level2,
                                })

    n = max(1, st['reports'])
    print('=' * 72)
    print('定向页 + 句式拆分：可构造样本量')
    print('=' * 72)
    print(f"  目标页 {st['pages_target']} / 全量 12481（约 17%）")
    print(f"  事实行（本期+上期都有值）      : {st['facts']}")
    print()
    print(f"  ★ 增长率类（本期值+增长词同句）: {st['growth_claims']}  → 每份 {st['growth_claims']/n:.1f}")
    print(f"    每条需要 {{本期, 上期}} 两个来源（集合评价）")
    print(f"      · 指标名精确出现在句中 : {st['growth_level_exact']}")
    print(f"      · 去修饰后命中(core)   : {st['growth_level_core']}")
    print(f"      · 仅片段命中(fragment) : {st['growth_level_fragment']}  ← 需人工确认是否同一口径")
    print(f"  ★ 本期引用类（只有本期绝对值）  : {st['quote_claims']}  → 每份 {st['quote_claims']/n:.1f}")
    print()
    g, q = st['growth_claims'], st['quote_claims']
    print(f"  合计样本            : {g+q}")
    print(f"  涉及的「上期来源」数 : {g}（增长率类各需要一条上期）")
    print(f"  涉及的「本期来源」数 : {g+q}")
    print(f"  本期:上期来源比      : {g+q}:{g} = {(g+q)/max(1,g):.2f}:1")
    print()
    print('  换算：')
    per = (g + q) / n
    for tgt in (300, 500):
        print(f"    {tgt} 条样本 → 约需 {tgt/max(per,0.01):.0f} 份年报")
    print()
    print('  增长率类样例（可直接核对）：')
    seen = set()
    for r in growth_pairs:
        k = (r['company'], r['metric'])
        if k in seen:
            continue
        seen.add(k)
        print(f"    {r['company']} {r['metric'][:22]:22} 本期={r['current']:,} 上期={r['prior']:,} 单位={r['unit']}")
        print(f"       第{r['claim_page']}页「{r['claim_text'][:96]}」")
        if len(seen) >= 8:
            break

    out = ROOT / 'output' / 'sentence_split_probe.json'
    out.write_text(json.dumps({'stats': dict(st), 'growth': growth_pairs[:400],
                               'quote': current_pairs[:400]}, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    print(f'\n明细 → {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
