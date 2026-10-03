"""定向页发现：只在「主要财务指标」与「经营情况讨论与分析」两类页里找正文锚点。

为什么这样做
------------
对 50 份年报的独立审计显示：整份解析产生的 8214 条候选里，8087 条是
"只在表格里出现的附注明细行"（如"按组合计提坏账准备"），正文从不提及。
这些页不在训练目标范围内，解析它们是纯浪费。

年报章节结构高度规范，这两类页可以用标题正则定位：
  第二节 公司简介和主要财务指标        → 主要会计数据/主要财务指标表
  第三节 管理层讨论与分析 / 经营情况讨论与分析 → 叙述性论断（"实现营业收入X元，同比增Y%"）

本脚本度量：定向解析能拿到多少「正文锚点」，并与整份解析对比。
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

# ---- 章节定位 ----
# 关键财务指标页：第二节
SEC2_PAT = re.compile(r'第\s*二\s*节\s*公司简介和主要财务指标|主要会计数据和财务指标')
# 管理层讨论与分析：第三节（不同年份表述不同）
SEC3_PAT = re.compile(r'第\s*三\s*节\s*(?:管理层讨论与分析|经营情况讨论与分析)|'
                      r'管理层讨论与分析|经营情况讨论与分析')
# 目录页会命中标题，用页码连续性排除
TOC_PAT = re.compile(r'目\s*录')

NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
CUR_PAT = re.compile(r'本期|本报告期|本期数|本期金额|期末|期末数|本年度|报告期')
PRI_PAT = re.compile(r'上期|上年同期|上期数|上期金额|期初|期初数|上年度|去年同期')
CHG_PAT = re.compile(r'增减|变动|变动比例|同比|增幅')
YEAR_PAT = re.compile(r'^(20\d{2})\s*年?$')
UNIT_PAT = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')
METRIC_HINTS = ('收入', '成本', '费用', '利润', '资产', '负债', '现金', '合计', '总额',
                '余额', '每股', '负债率', '收益率', '流量', '毛利率', '净额')
# 叙述性论断的信号词（管理层讨论里的典型句式）
NARRATIVE = re.compile(r'实现|达到|完成|同比|较上年|较上期|增长|下降|提升|减少|增加|扭亏|为盈')


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


def clean_cell(c):
    return re.sub(r'\s+', '', str(c or ''))


def cell_kind(c):
    c = c.strip()
    if not c:
        return 'empty'
    if c.endswith('%'):
        return 'pct'
    return 'num' if re.match(r'^-?[\d,]+(?:\.\d+)?$', c) else 'text'


def locate_target_pages(pages):
    """返回 (关键指标页集合, 管理层讨论页集合)。"""
    sec2, sec3 = set(), set()
    n = len(pages)
    for idx, (pno, text) in enumerate(pages):
        head = text[:600]              # 章节标题一般在页首
        # 跳过目录页
        if TOC_PAT.search(head) and idx < 8:
            continue
        if SEC2_PAT.search(head):
            # 关键指标通常占标题页后 2-4 页
            sec2.update(range(pno, min(n, pno + 4) + 1))
        if SEC3_PAT.search(head):
            sec3.update(range(pno, min(n, pno + 30) + 1))
    return sec2, sec3


def classify_columns(header_rows, ncols):
    col_text = []
    for c in range(ncols):
        parts = [r[c] for r in header_rows if c < len(r) and r[c]]
        col_text.append(' '.join(parts))
    roles = ['unknown'] * ncols
    if col_text and (not NUM.search(col_text[0]) or len(col_text[0]) > 2):
        roles[0] = 'metric'
    explicit_cur = [c for c, t in enumerate(col_text) if c and CUR_PAT.search(t) and not CHG_PAT.search(t)]
    explicit_pri = [c for c, t in enumerate(col_text) if c and PRI_PAT.search(t) and not CHG_PAT.search(t)]
    years = {c: int(YEAR_PAT.match(t.strip()).group(1))
             for c, t in enumerate(col_text) if c and YEAR_PAT.match(t.strip())}
    if explicit_cur and explicit_pri:
        roles[explicit_cur[0]] = 'current'
        roles[explicit_pri[0]] = 'prior'
        method = 'explicit'
    elif len(years) >= 2:
        ordered = sorted(years.items(), key=lambda kv: -kv[1])
        roles[ordered[0][0]] = 'current'
        roles[ordered[1][0]] = 'prior'
        method = 'year'
    else:
        method = 'unresolved'
    for c, t in enumerate(col_text):
        if c and CHG_PAT.search(t) and roles[c] == 'unknown':
            roles[c] = 'change'
    return roles, method, col_text


def header_rows(table, max_scan=4):
    out = []
    for i, row in enumerate(table[:max_scan]):
        cells = [clean_cell(c) for c in row]
        kinds = [cell_kind(c) for c in cells]
        nnum = sum(1 for k in kinds if k in ('num', 'pct'))
        ntext = sum(1 for k in kinds if k == 'text')
        nfill = sum(1 for k in kinds if k != 'empty')
        if nfill >= 3 and nnum >= 2 and ntext >= 1 and i > 0:
            break
        out.append(cells)
    return out or [[clean_cell(c) for c in table[0]]]


def main() -> int:
    import pymupdf
    import pdfplumber

    rows = [json.loads(l) for l in (CORPUS / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    stats = Counter()
    per_report = []
    samples = []

    for rec in rows:
        pdf = CORPUS / rec['local_file']
        if not pdf.is_file():
            continue
        doc = pymupdf.open(pdf)
        pages = [(i + 1, (p.get_text('text') or '')) for i, p in enumerate(doc)]
        doc.close()
        sec2, sec3 = locate_target_pages(pages)
        target = sec2 | sec3
        stats['reports'] += 1
        stats['pages_total'] += len(pages)
        stats['pages_target'] += len(target)
        if sec2:
            stats['sec2_found'] += 1
        if sec3:
            stats['sec3_found'] += 1

        # 目标页里的正文句子（全文档建一次，按页取）
        sents_by_page = {}
        for pno, txt in pages:
            if pno not in target:
                continue
            for s in re.split(r'[。！？；;\n]', txt):
                s = re.sub(r'\s+', ' ', s).strip()
                if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
                    sents_by_page.setdefault(pno, []).append(s)

        # 只在目标页解析表格
        with pdfplumber.open(pdf) as pdf:
            for pno in sorted(target):
                if pno < 1 or pno > len(pdf.pages):
                    continue
                page = pdf.pages[pno - 1]
                ptext = pages[pno - 1][1]
                unit_m = UNIT_PAT.search(ptext)
                unit = unit_m.group(2) if unit_m else None
                if unit:
                    stats['pages_with_unit'] += 1
                for t in (page.extract_tables() or []):
                    if not t or len(t) < 2:
                        continue
                    hr = header_rows(t)
                    flat = ' '.join(c for row in hr for c in row)
                    has_period = bool(CUR_PAT.search(flat) or len(re.findall(r'20\d{2}', flat)) >= 2)
                    if not (has_period and (PRI_PAT.search(flat) or len(re.findall(r'20\d{2}', flat)) >= 2)):
                        continue
                    stats['tables'] += 1
                    ncols = max(len(r) for r in t)
                    roles, method, col_text = classify_columns(hr, ncols)
                    stats[f'method_{method}'] += 1
                    cur_c = next((c for c, r in enumerate(roles) if r == 'current'), None)
                    pri_c = next((c for c, r in enumerate(roles) if r == 'prior'), None)
                    chg_c = next((c for c, r in enumerate(roles) if r == 'change'), None)
                    if cur_c is None or pri_c is None:
                        continue
                    for row in t:
                        cells = [clean_cell(c) for c in row]
                        if not cells or not cells[0] or len(cells[0]) > 30:
                            continue
                        metric = cells[0]
                        if not any(k in metric for k in METRIC_HINTS):
                            continue
                        def at(c):
                            return cells[c] if c is not None and c < len(cells) else ''
                        cur, pri = canon(at(cur_c)), canon(at(pri_c))
                        if cur is None or pri is None:
                            stats['rows_missing_value'] += 1
                            continue
                        stats['fact_rows'] += 1
                        # 三值
                        chg = canon(at(chg_c)) if chg_c is not None else None
                        if chg is not None and pri != 0 and abs(chg) <= 1000:
                            calc = round((cur - pri) / abs(pri) * 100, 2)
                            if abs(calc - chg) <= max(0.05, abs(chg) * 0.05):
                                stats['three_consistent'] += 1
                            else:
                                stats['three_inconsistent'] += 1
                        # 正文锚点：只在目标页找
                        def anchor(value):
                            for sp, ss in sents_by_page.items():
                                for s in ss:
                                    if metric[:6] in s and value is not None \
                                            and any(canon(m.group(0)) == value for m in NUM.finditer(s)):
                                        return sp, s
                            return None, None

                        cp_cur, s_cur = anchor(cur)
                        cp_pri, s_pri = anchor(pri)
                        if s_cur and NARRATIVE.search(s_cur):
                            stats['anchor_current'] += 1
                            if len(samples) < 12:
                                samples.append({'company': rec['stock_code'], 'metric': metric,
                                                'period': '本期', 'value': cur, 'unit': unit,
                                                'claim_page': cp_cur, 'claim_text': s_cur})
                        if s_pri and NARRATIVE.search(s_pri):
                            stats['anchor_prior'] += 1
                            if len(samples) < 12:
                                samples.append({'company': rec['stock_code'], 'metric': metric,
                                                'period': '上期', 'value': pri, 'unit': unit,
                                                'claim_page': cp_pri, 'claim_text': s_pri})
                        if s_cur and not s_pri and NARRATIVE.search(s_cur):
                            stats['growth_dual'] += 0  # 占位，见下
                        # 双来源：同一句里同时出现本期值与增长词
                        if s_cur and re.search(r'同比|较上年|较上期|增减|变动|增长|下降', s_cur):
                            stats['growth_sentence'] += 1

        per_report.append({
            'company': rec['stock_code'],
            'pages': len(pages),
            'pages_sec2': len(sec2),
            'pages_sec3': len(sec3),
            'pages_target': len(target),
        })

    n = max(1, stats['reports'])
    print('=' * 74)
    print(f"定向解析 {stats['reports']} 份年报：只处理 {stats['pages_target']} 页 / 共 {stats['pages_total']} 页 "
          f"({stats['pages_target']/max(1,stats['pages_total'])*100:.0f}%)")
    print('=' * 74)
    print(f"  定位到「主要财务指标」节的报告 : {stats['sec2_found']}/{n}")
    print(f"  定位到「管理层讨论与分析」节的 : {stats['sec3_found']}/{n}")
    print(f"  目标页内声明了单位的页         : {stats['pages_with_unit']}")
    print()
    print(f"  带期间列的表                   : {stats['tables']}")
    print(f"    其中显式期间标签             : {stats['method_explicit']}")
    print(f"    其中按年份推断               : {stats['method_year']}")
    print(f"  事实行（本期+上期都有值）      : {stats['fact_rows']}")
    print(f"    本期或上期缺值（丢弃）       : {stats['rows_missing_value']}")
    print()
    print(f"  三值自洽                       : {stats['three_consistent']}")
    print(f"  三值矛盾                       : {stats['three_inconsistent']}")
    print()
    print(f"  ★ 本期锚点（正文句子+叙述词）  : {stats['anchor_current']}")
    print(f"  ★ 上期锚点（正文句子+叙述词）  : {stats['anchor_prior']}")
    print(f"  ★ 增长率句（本期值+增长词）    : {stats['growth_sentence']}")
    print()
    tot_anchor = stats['anchor_current'] + stats['anchor_prior']
    print(f"  合计可用锚点 {tot_anchor}，平均 {tot_anchor/n:.1f} 条/份")
    if stats['anchor_current'] and stats['anchor_prior']:
        print(f"  本期:上期 = {stats['anchor_current']}:{stats['anchor_prior']} "
              f"= {stats['anchor_current']/stats['anchor_prior']:.1f}:1")
    print()
    print('  换算到目标规模：')
    per = tot_anchor / n
    for target in (300, 500):
        print(f"    {target} 条正例 → 约需 {target/max(per,0.01):.0f} 份年报")
    print()
    print('  锚点样例：')
    for s in samples[:8]:
        print(f"    {s['company']} {s['metric'][:16]:16} {s['period']} = {s['value']} {s['unit']}")
        print(f"       第{s['claim_page']}页「{s['claim_text'][:96]}」")

    out = ROOT / 'output' / 'targeted_page_probe.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'stats': dict(stats), 'per_report': per_report,
                              'samples': samples}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n明细 → {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
