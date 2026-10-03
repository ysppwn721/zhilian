"""定向页论断—事实候选构造器（v2，重写版）。

为什么只解析两类页
------------------
对 50 份年报的审计显示：整份解析产生的 8214 条候选里 8087 条是
"只在表格里出现的附注明细行"，正文从不提及——它们不是论断样本。
真正的论断集中在：
  第二节 公司简介和主要财务指标   → 主要会计数据表
  第三节 管理层讨论与分析         → 叙述性论断
实测这两类页约占全文 17%，解析成本降到 1/6。

句式拆分（关键设计）
--------------------
中文年报正文的典型句式是：
    报告期内，公司实现营业收入 X 元，较上年同期增加 Y%
它同时给出**本期绝对值**与**增长率**，但**不给上期绝对值**（上期在表格第二列）。
因此样本分两类：
  growth：正例来源集合 = {本期事实, 上期事实}（两个都对，用集合指标评价）
  quote ：正例 = 本期事实
这样上期来源不再"缺失"，本期:上期来源 ≈ 1.5:1（旧构造器为 8.7:1）。

本版相对上一版的修正（都是实测踩出来的）
----------------------------------------
1. 单份 PDF 损坏不再让整批崩溃（曾因 PdfminerException: Unexpected EOF 丢掉 18 分钟进度）
2. 每 10 份写一次 candidates_v2.partial.jsonl，支持断点续跑
3. 表标题按**表格自身位置**取：自下而上最多 6 行，跳过单位行/噪声/目录行/勾选框行，
   并要求含表名特征词——旧版会取到「第十节财务报告....」「一、公司信息」这类无关文本
4. 其他年份列（三年表的 2021/2022 列）写入输出，供构造"错期间"负例
5. 三值校验区分"仅符号不同"（年报用文字表达方向、数字取绝对值）与真矛盾

用法：
    python packaging/build_candidates_v2.py --corpus 答辩评测/annual_reports_all
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = ROOT / '答辩评测' / 'annual_reports_all'

NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
UNIT_PAT = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')
UNIT_SUFFIX_PAT = re.compile(r'[（(]\s*(元/股|元|万元|亿元|千元|%|万TEU)\s*[）)]')
CUR_PAT = re.compile(r'本期|本报告期|本期数|本期金额|本期发生额|期末|期末数|期末余额|本年度|报告期')
PRI_PAT = re.compile(r'上期|上年同期|上期数|上期金额|上年同期数|期初|期初数|期初余额|上年度|去年同期')
CHG_PAT = re.compile(r'增减|变动|变动比例|同比|增幅|变化')
YEAR_PAT = re.compile(r'^(20\d{2})\s*年?$')
GROWTH_WORD = re.compile(r'同比|较上年|较上期|增减|变动|增长|下降|上升|减少|增加|增幅')

SEC2_PAT = re.compile(r'第\s*二\s*节\s*公司简介和主要财务指标|主要会计数据和财务指标')
SEC3_PAT = re.compile(r'第\s*三\s*节\s*(?:管理层讨论与分析|经营情况讨论与分析)|管理层讨论与分析|经营情况讨论与分析')
TOC_PAT = re.compile(r'目\s*录')

TITLE_HINT = re.compile(r'表|情况|分析|构成|变动|指标|数据|状况|摘要|会计')
UNIT_LINE_PAT = re.compile(r'^单位\s*[:：]')
CAPTION_NOISE = re.compile(r'^[\d\s,.%()（）\-—/]+$|^\d+\.?$|^[（(]\d+[）)]')
CHECKBOX_PAT = re.compile(r'^[□☑☐√是是否否\s]+$')

SYNONYM_GROUPS = [
    ('营业收入', '营收', '销售收入', '主营收入', '营业总收入'),
    ('营业成本', '经营成本', '销售成本'),
    ('净利润', '纯利润', '归母净利润'),
]
NOISE_METRIC = re.compile(r'^(合计|小计|其中|附注|说明|序号|项目|科目|单位)$')
UNIT_FACTORS = {'元': 1.0, '千元': 1_000.0, '万元': 10_000.0, '亿元': 100_000_000.0}


def canon(x):
    """数值归一化：去千分位、括号负数、全角负号。"""
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


def clean(c) -> str:
    return re.sub(r'[\s\u3000]+', '', str(c or ''))


def monetary_factor(unit: str | None) -> float | None:
    return UNIT_FACTORS.get(unit or '')


def _line_texts(page_words):
    """把 pdfplumber 单词按版面行聚合，供表格附近的单位定位。"""
    groups = {}
    for word in page_words:
        groups.setdefault(round(float(word['top']), 1), []).append(word)
    for top, words in groups.items():
        words = sorted(words, key=lambda w: w['x0'])
        yield top, max(float(w['bottom']) for w in words), ''.join(w['text'] for w in words)


def table_unit(page_words, bbox, fallback=None):
    """优先取当前表格上方最近的单位行，避免一页多表时串用页面单位。"""
    if not bbox:
        return fallback
    candidates = []
    for _top, bottom, text in _line_texts(page_words):
        if bottom > bbox[1] + 3:
            continue
        match = UNIT_PAT.search(text)
        unit = match.group(2) if match else None
        if not unit:
            suffix = UNIT_SUFFIX_PAT.search(text)
            unit = suffix.group(1) if suffix else None
        if unit and bbox[1] - bottom <= 220:
            candidates.append((bbox[1] - bottom, unit))
    return min(candidates, key=lambda item: item[0])[1] if candidates else fallback


def _decimal_places(raw_text: str) -> int:
    raw = raw_text.replace(',', '').replace('−', '-').replace('－', '-')
    raw = raw.strip('()%')
    return len(raw.split('.', 1)[1]) if '.' in raw else 0


def sentence_values(sentence: str):
    """返回句中数值及其显示单位，金额同时保留换算到元的值。"""
    values = []
    for match in NUM.finditer(sentence):
        raw_text = match.group(0)
        raw = canon(raw_text)
        if raw is None:
            continue
        tail = sentence[match.end():match.end() + 10]
        if raw_text.endswith('%') or tail.lstrip().startswith('%'):
            values.append((raw, raw, '%', _decimal_places(raw_text), 1.0))
            continue
        unit_match = re.match(r'\s*(亿元|万元|千元|元|万TEU)', tail)
        mentioned = unit_match.group(1) if unit_match else None
        factor = monetary_factor(mentioned) if mentioned else None
        base = raw * factor if factor else raw
        values.append((raw, base, mentioned, _decimal_places(raw_text), factor or 1.0))
    return values


def value_in_sentence(target: float, target_unit: str | None, sentence: str) -> bool:
    """按显示单位换算后匹配，允许年报金额因四舍五入产生的误差。"""
    target_factor = monetary_factor(target_unit)
    target_base = target * target_factor if target_factor else target
    for raw, base, mentioned, decimals, factor in sentence_values(sentence):
        if target_factor and mentioned in UNIT_FACTORS:
            tolerance = max(0.5 * (10 ** (-decimals)) * factor, abs(target_base) * 1e-7, 0.01)
            if abs(base - target_base) <= tolerance:
                return True
        elif abs(raw - target) <= max(0.01, abs(target) * 1e-7):
            return True
    return False


def cell_kind(c: str) -> str:
    c = c.strip()
    if not c:
        return 'empty'
    if c.endswith('%'):
        return 'pct'
    return 'num' if re.match(r'^-?[\d,]+(?:\.\d+)?$', c) else 'text'


def compact_columns(table):
    """压掉全空列。

    pdfplumber 会把列间距大的表抽成大量空列：实测 002124 第 10 页「主要会计数据」表
    真实只有 6 列，却被抽成 **21 列**，每个真实列后面跟 2 个空列：
        ['', '', '', '', '2023年', '', '', '本年比上年', '', '', '', '', ...]
        ['', '营业收入（元）', '', '10,231,927,988.44', '', '', '9,570,942,144.13', ...]
    表头文本本身能被匹配到，但列索引整体错位，导致数据行判定与取值都失败。
    这是 8309 张表里 4949 张（60%）列角色判定失败的直接原因。
    """
    if not table:
        return table
    ncols = max(len(r) for r in table)
    keep = [c for c in range(ncols)
            if any(clean(row[c]) for row in table if c < len(row))]
    if not keep or len(keep) == ncols:
        return table
    return [[row[c] if c < len(row) else None for c in keep] for row in table]


def header_rows(t, max_scan=6):
    """定位表头行：开头连续若干行，直到出现数据行特征。

    多层表头（如追溯调整表的「调整前/调整后」）可能占 3-4 行，故放宽到 6。
    """
    out = []
    for i, row in enumerate(t[:max_scan]):
        cells = [clean(c) for c in row]
        ks = [cell_kind(c) for c in cells]
        if (sum(1 for k in ks if k != 'empty') >= 3
                and sum(1 for k in ks if k in ('num', 'pct')) >= 2
                and sum(1 for k in ks if k == 'text') >= 1 and i > 0):
            break
        out.append(cells)
    return out or [[clean(c) for c in t[0]]]


def classify(hr, ncols):
    """列角色：metric/current/prior/change/other_year/unknown。

    优先显式「本期/上期」标签；否则按年份推断（最大年份=本期，次大=上期，
    其余年份标 other_year——它们是"错期间"负例的唯一真实来源）。
    """
    col = [' '.join(r[c] for r in hr if c < len(r) and r[c]) for c in range(ncols)]
    roles = ['unknown'] * ncols
    if col and len(col[0]) > 2:
        roles[0] = 'metric'
    ec = [c for c, t in enumerate(col) if c and CUR_PAT.search(t) and not CHG_PAT.search(t)]
    ep = [c for c, t in enumerate(col) if c and PRI_PAT.search(t) and not CHG_PAT.search(t)]
    yr = {c: int(YEAR_PAT.match(t.strip()).group(1)) for c, t in enumerate(col)
          if c and YEAR_PAT.match(t.strip())}
    if ec and ep:
        roles[ec[0]] = 'current'
        roles[ep[0]] = 'prior'
        method = 'explicit_period_label'
    elif len(yr) >= 2:
        o = sorted(yr.items(), key=lambda kv: -kv[1])
        roles[o[0][0]] = 'current'
        roles[o[1][0]] = 'prior'
        method = 'year_header_inference'
    else:
        method = 'unresolved'
    chg = next((c for c, t in enumerate(col) if c and CHG_PAT.search(t)), None)
    if chg is not None and roles[chg] == 'unknown':
        roles[chg] = 'change'
    other_years = {}
    for c, y in yr.items():
        if roles[c] == 'unknown':
            roles[c] = 'other_year'
            other_years[c] = y
    return roles, method, col, chg, other_years


def metric_match_level(metric, sentence):
    """指标名与正文用词的匹配强度：exact / core / fragment / ''。

    分级是为了把"放宽匹配"的代价量化出来，而不是假装没有代价。
    """
    if not metric or not sentence:
        return ''
    if metric in sentence:
        return 'exact'
    core = metric
    for noise in ('（元）', '(元)', '（%）', '(%)', '变动原因说明', '总额', '净额',
                  '归属于上市公司股东的', '归属于母公司股东的', '产生的', '其中：', '：'):
        core = core.replace(noise, '')
    core = core.strip('：: ')
    if core and core in sentence:
        return 'core'
    for seg in re.findall(r'[\u4e00-\u9fff]{3,}', core):
        if seg in sentence:
            return 'fragment'
    return ''


def locate(pages):
    """定位「主要财务指标」与「管理层讨论与分析」两类页。"""
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


def title_above(page_words, bbox):
    """表格上边界以上，按行回溯找第一个「像表标题」的行。

    find_tables() 检出的是**版式表格**，会包含「公司信息」「联系方式」这类非财务块，
    甚至目录页。因此不能只取紧邻那一行——实测会取到「第十节财务报告....」。
    做法：自下而上扫描最多 6 行，跳过单位行/噪声/目录行/勾选框行，并要求含表名特征词。
    """
    if not bbox:
        return ''
    top = bbox[1]
    above = [w for w in page_words if w['bottom'] <= top + 2]
    if not above:
        return ''
    tops = sorted({round(w['top'], 1) for w in above}, reverse=True)
    for tp in tops[:6]:
        lw = sorted((w for w in above if abs(w['top'] - tp) <= 1.5), key=lambda w: w['x0'])
        text = ''.join(w['text'] for w in lw).strip()
        if len(text) < 3 or len(text) > 60:
            continue
        if UNIT_LINE_PAT.match(text) or CAPTION_NOISE.match(text) or CHECKBOX_PAT.match(text):
            continue
        if text.count('.') >= 5 or text.count('…') >= 3:
            continue
        if re.search(r'适用|不适用', text):
            continue
        if re.search(r'[\u4e00-\u9fff]\s*[，,。]\s*$', text) and len(text) > 22:
            continue
        if not TITLE_HINT.search(text):
            continue
        return text
    return ''


def scope_of(caption, col_text):
    """口径提示：合并/母公司/分部/未标明。只看标题与表头，不猜页面正文。"""
    ctx = (caption or '') + ' ' + ' '.join(col_text)
    bits = []
    if re.search(r'合并', ctx):
        bits.append('合并')
    if re.search(r'母公司|公司本部', ctx):
        bits.append('母公司')
    if re.search(r'分部|业务板块|分产品|分行业|分地区|营业收入构成|成本构成', ctx):
        bits.append('分部')
    return '/'.join(bits) if bits else '未标明'


def flush_partial(corpus: Path, out: list) -> None:
    (corpus / 'candidates_v2.partial.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in out), encoding='utf-8')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 份（0=全部）')
    args = ap.parse_args()

    import pymupdf
    import pdfplumber

    corpus = args.corpus
    rows = [json.loads(l) for l in (corpus / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()
            if l.strip()]
    if args.limit:
        rows = rows[:args.limit]

    out, stats, skipped_files = [], Counter(), []
    t0 = time.time()
    print(f'构造 {len(rows)} 份，开始 {time.strftime("%H:%M:%S")}', flush=True)

    for idx, rec in enumerate(rows, 1):
        pdf_path = corpus / rec['local_file']
        if not pdf_path.is_file():
            continue

        try:
            doc = pymupdf.open(pdf_path)
            pages = [(i + 1, (p.get_text('text') or '')) for i, p in enumerate(doc)]
            doc.close()
        except Exception as exc:
            stats['skipped_pymupdf'] += 1
            skipped_files.append({'company': rec['stock_code'], 'file': rec['local_file'],
                                  'stage': 'pymupdf', 'error': type(exc).__name__})
            print(f'  [跳过] {rec["stock_code"]}: {type(exc).__name__}', flush=True)
            continue

        sec2, sec3 = locate(pages)
        target = sec2 | sec3
        stats['pages_target'] += len(target)

        sents = []
        for pno, txt in pages:
            if pno not in target:
                continue
            for s in re.split(r'[。！？；;\n]', txt):
                s = re.sub(r'\s+', ' ', s).strip()
                if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
                    sents.append((pno, s))

        try:
            with pdfplumber.open(pdf_path) as pdf:
                for pno in sorted(target):
                    if pno < 1 or pno > len(pdf.pages):
                        continue
                    page = pdf.pages[pno - 1]
                    ptext = pages[pno - 1][1]
                    um = UNIT_PAT.search(ptext)
                    page_unit = um.group(2) if um else None
                    page_words = page.extract_words(use_text_flow=False) or []
                    table_objs = page.find_tables() or []
                    page_unit_line = ''
                    for line in reversed(ptext.splitlines()):
                        cand = line.strip()
                        if UNIT_LINE_PAT.match(cand):
                            page_unit_line = cand
                            break

                    for t_idx, t in enumerate(page.extract_tables() or []):
                        if not t or len(t) < 2:
                            continue
                        # 压掉全空列（必须在 header_rows/classify 之前）。
                        # pdfplumber 会把列间距大的表抽成大量空列：实测 002124 第 10 页
                        # 「主要会计数据」表真实 6 列被抽成 21 列，每个真实列后跟 2 个空列，
                        # 表头与数据行因此整体错位。这是 8309 张表里 4949 张（60%）
                        # 判定失败的主因。
                        t = compact_columns(t)
                        bbox = table_objs[t_idx].bbox if t_idx < len(table_objs) else None
                        unit = table_unit(page_words, bbox, page_unit)
                        raw_title = title_above(page_words, bbox)
                        if raw_title:
                            caption = raw_title
                        elif page_unit_line:
                            caption = page_unit_line
                        else:
                            caption = ''

                        hr = header_rows(t)
                        flat = ' '.join(c for r in hr for c in r)
                        has_cur = bool(CUR_PAT.search(flat)) or len(re.findall(r'20\d{2}', flat)) >= 2
                        has_pri = bool(PRI_PAT.search(flat)) or len(re.findall(r'20\d{2}', flat)) >= 2
                        if not (has_cur and has_pri):
                            continue

                        roles, method, col_text, chg_c, other_years = classify(hr, max(len(r) for r in t))
                        cc = next((c for c, r in enumerate(roles) if r == 'current'), None)
                        pc = next((c for c, r in enumerate(roles) if r == 'prior'), None)
                        if cc is None or pc is None:
                            stats['tables_unresolved'] += 1
                            continue
                        stats['tables'] += 1
                        scope = scope_of(caption, col_text)

                        table_facts = []
                        for row in t:
                            cells = [clean(c) for c in row]
                            if not cells or len(cells[0]) < 2:
                                continue
                            m = cells[0]
                            if NOISE_METRIC.match(m) or re.fullmatch(r'[\d\s,.%()（）\-—/]+', m):
                                continue
                            cur = canon(cells[cc]) if cc < len(cells) else None
                            pri = canon(cells[pc]) if pc < len(cells) else None
                            chg = canon(cells[chg_c]) if chg_c is not None and chg_c < len(cells) else None
                            if cur is None or pri is None:
                                continue
                            rf = {'metric': m, 'current': cur, 'prior': pri, 'change': chg,
                                  'scope': scope, 'page': pno, 'column_method': method,
                                  'unit': unit, 'caption': caption[:120]}
                            extra = {y: canon(cells[c]) for c, y in (other_years or {}).items()
                                     if c < len(cells) and canon(cells[c]) is not None}
                            if extra:
                                rf['other_years'] = extra
                                stats['other_year_values'] += len(extra)
                            table_facts.append(rf)
                        stats['fact_rows'] += len(table_facts)

                        for f in table_facts:
                            metric = f['metric']
                            three, calc = 'no_change_value', None
                            if f['change'] is not None and abs(f['change']) <= 1000 and f['prior'] != 0:
                                calc = round((f['current'] - f['prior']) / abs(f['prior']) * 100, 2)
                                tol = max(0.05, abs(f['change']) * 0.05)
                                if abs(calc - f['change']) <= tol:
                                    three = 'consistent'
                                elif abs(abs(calc) - abs(f['change'])) <= tol:
                                    three = 'sign_convention_differs'
                                else:
                                    three = 'inconsistent'
                            elif f['change'] is not None:
                                three = 'change_looks_like_amount'

                            growth_hit = quote_hit = name_only = None
                            level = ''
                            for sp, s in sents:
                                lv = metric_match_level(metric, s)
                                if not lv:
                                    continue
                                if not value_in_sentence(f['current'], f.get('unit'), s):
                                    if name_only is None and NUM.search(s) and re.search(
                                            r'[，,。！？；]|实现|达到|完成|为|较|同比|增长|下降|其中|公司|报告期|说明', s):
                                        name_only = (sp, s)
                                    continue
                                if GROWTH_WORD.search(s):
                                    growth_hit, level = (sp, s), lv
                                    break
                                if quote_hit is None:
                                    quote_hit, level = (sp, s), lv

                            syn = [o for grp in SYNONYM_GROUPS if metric in grp
                                   for o in grp if o != metric]
                            kind = ('growth' if growth_hit else
                                    ('quote' if quote_hit else
                                     ('abstain' if name_only else 'table_only')))
                            claim_page, claim_text = (growth_hit or quote_hit or name_only or (None, None))
                            stats[f'kind_{kind}'] += 1

                            negatives = sum(1 for g in table_facts
                                            if g is not f and (g['metric'] != metric
                                                               or g['scope'] != f['scope']))
                            out.append({
                                'company': rec['stock_code'],
                                'company_name': rec['company_name'],
                                'source_file': rec['local_file'],
                                'claim_text': claim_text,
                                'claim_page': claim_page,
                                'metric': metric,
                                'period': '本期',
                                'value': f['current'],
                                'unit': f['unit'],
                                'scope': f['scope'],
                                'fact_page': f['page'],
                                'fact_prior_value': f['prior'],
                                'fact_change_value': f['change'],
                                'other_years': f.get('other_years') or None,
                                'column_method': f['column_method'],
                                'three_value_check': three,
                                'three_value_calc': calc,
                                'match_level': level or None,
                                'claim_kind': kind,
                                'source_set': ([f'{{本期:{metric}}}', f'{{上期:{metric}}}']
                                               if kind == 'growth' else
                                               ([f'{{本期:{metric}}}'] if kind == 'quote' else [])),
                                'negative_pool_size': negatives,
                                'synonym_candidates': syn or None,
                                'caption': f['caption'],
                                'label': None,
                                'audit_status': ('needs_human_review' if kind in ('growth', 'quote')
                                                 else ('abstain_sample' if kind == 'abstain'
                                                       else 'table_only_no_claim')),
                            })
        except Exception as exc:
            stats['skipped_pdfplumber'] += 1
            skipped_files.append({'company': rec['stock_code'], 'file': rec['local_file'],
                                  'stage': 'pdfplumber', 'error': type(exc).__name__})
            print(f'  [跳过] {rec["stock_code"]}: {type(exc).__name__}', flush=True)

        if idx % 10 == 0 or idx == len(rows):
            flush_partial(corpus, out)
            print(f'  [{idx}/{len(rows)}] {rec["stock_code"]} '
                  f'{time.strftime("%H:%M:%S")} 候选 {len(out)}', flush=True)

    (corpus / 'candidates_v2.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in out), encoding='utf-8')
    flat_cols = ['company', 'company_name', 'source_file', 'claim_text', 'claim_page', 'metric',
                 'period', 'value', 'unit', 'scope', 'fact_page', 'fact_prior_value',
                 'fact_change_value', 'column_method', 'three_value_check', 'three_value_calc',
                 'match_level', 'claim_kind', 'negative_pool_size', 'caption', 'label', 'audit_status']
    with (corpus / 'candidates_v2.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=flat_cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(out)
    (corpus / 'candidates_v2_stats.json').write_text(
        json.dumps(dict(stats), ensure_ascii=False, indent=2), encoding='utf-8')
    if skipped_files:
        (corpus / 'candidates_v2_skipped.jsonl').write_text(
            '\n'.join(json.dumps(s, ensure_ascii=False) for s in skipped_files), encoding='utf-8')

    kinds = Counter(c['claim_kind'] for c in out)
    n = max(1, len(rows))
    print()
    print('=' * 70)
    print(f'目标页 {stats["pages_target"]} · 事实行 {stats["fact_rows"]} · '
          f'其他年份数值 {stats["other_year_values"]}')
    print(f'  增长率类 growth  : {kinds["growth"]:>4}  ({kinds["growth"]/n:.2f}/份)')
    print(f'  本期引用 quote   : {kinds["quote"]:>4}  ({kinds["quote"]/n:.2f}/份)')
    print(f'  拒答   abstain   : {kinds["abstain"]:>4}')
    print(f'  仅表格 table_only: {kinds["table_only"]:>4}  (不进入训练集)')
    usable = kinds['growth'] + kinds['quote']
    print(f'  可用候选          : {usable}')
    print(f'  本期:上期来源     = {usable}:{kinds["growth"]} = {usable/max(1,kinds["growth"]):.2f}:1')
    if skipped_files:
        print(f'  跳过 {len(skipped_files)} 份（PDF 损坏等），见 candidates_v2_skipped.jsonl')
    print(f'  用时 {(time.time()-t0)/60:.1f} 分钟')
    print('=' * 70)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
