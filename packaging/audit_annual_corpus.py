"""对 annual_reports_new_20261002 做训练前独立审计。

只读：不修改语料目录、不修改冻结评测集、不训练、不产生准确率数字。

严格按 GPT 任务书的 10 项逐份检查，输出：
  - audit_report.md     审计报告
  - audit_candidates.jsonl  待审计候选（label=null，audit_status 标注）
  - audit_rejected.jsonl    不能进入训练集的记录及原因

关键实现说明（上一轮验证脚本的缺口）：
  上一轮只验证了「数值能否在正文找到」，**没有做列角色分类**。
  本脚本从头实现多行表头解析，并如实报告其识别结果与失败形态。
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / '答辩评测' / 'annual_reports_new_20261002'

NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
# 单元格形态判定
CELL_MONEY = re.compile(r'^-?[\d,]+(?:\.\d+)?$')
CELL_PCT = re.compile(r'^-?[\d,]+(?:\.\d+)?%$|^-?\d+(?:\.\d+)?$')

CUR_PAT = re.compile(r'本期|本报告期|本期数|本期金额|本期发生额|期末|期末数|期末余额|本年度|报告期')
PRI_PAT = re.compile(r'上期|上年同期|上期数|上期金额|上年同期数|期初|期初数|期初余额|上年度|去年同期')
CHG_PAT = re.compile(r'增减|变动|变动比例|同比|增幅|变化|增长')
# 年份表头：真实年报常用「2023年 / 2022年 / 2021年」而非「本期/上期」
YEAR_PAT = re.compile(r'^(20\d{2})\s*年?$')
UNIT_PAT = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')
UNIT_TOKEN = re.compile(r'(万元|亿元|千元|元)')
MERGE_PAT = re.compile(r'合并')
PARENT_PAT = re.compile(r'母公司|公司本部|本部')
SEG_PAT = re.compile(r'分部|业务板块|分产品|分行业|分地区')
METRIC_HINTS = ('收入', '成本', '费用', '利润', '资产', '负债', '现金', '合计', '总额',
                '余额', '每股', '负债率', '收益率', '流量')
# 需要人工裁定的同义指标（任务书第 7 项：不得自动判定等价）
SYNONYM_GROUPS = [
    ('营业收入', '营收', '销售收入', '主营收入', '营业总收入'),
    ('营业成本', '经营成本', '销售成本'),
    ('净利润', '纯利润', '归母净利润', '归属于母公司股东的净利润'),
    ('销售费用', '销售支出'),
    ('管理费用', '管理支出'),
    ('研发费用', '研发支出'),
]


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


def cell_kind(c: str) -> str:
    c = c.strip()
    if not c:
        return 'empty'
    if c.endswith('%'):
        return 'pct'
    if CELL_MONEY.match(c):
        return 'num'
    return 'text'


def clean_cell(c) -> str:
    return re.sub(r'\s+', '', str(c or ''))


def find_header_rows(table, max_scan=4):
    """定位表头行：取开头连续若干行，直到某行看起来是数据行。

    真实年报常见两行表头，例如：
        行0: ['主要会计数据','2023年','2022年','本期比上年同期增减(%)','2021年']
        行1: ['营业收入','5,368,887,974.75','5,162,609,563.77','4.00','4,716,577,077.85']
    因此不能只看第一行、也不能假定只有一行。
    """
    header_rows = []
    for i, row in enumerate(table[:max_scan]):
        cells = [clean_cell(c) for c in row]
        kinds = [cell_kind(c) for c in cells]
        n_num = sum(1 for k in kinds if k in ('num', 'pct'))
        n_text = sum(1 for k in kinds if k == 'text')
        n_filled = sum(1 for k in kinds if k != 'empty')
        # 数据行特征：中段出现连续数值，且首个非空单元格是文本
        if n_filled >= 3 and n_num >= 2 and n_text >= 1 and i > 0:
            break
        header_rows.append(cells)
    return header_rows or [[clean_cell(c) for c in table[0]]]


def classify_columns(header_rows, ncols, page_text, table_caption=''):
    """把列映射到角色。返回 (roles, detail, issues)。

    角色：metric / current / prior / change / other / unknown
    只用表头文本做判定（不猜数值），并把判定依据一并返回，便于审计复核。
    """
    roles = ['unknown'] * ncols
    detail = {}
    issues = []

    # 表头文本按列拼接
    col_text = []
    for c in range(ncols):
        parts = [row[c] for row in header_rows if c < len(row) and row[c]]
        col_text.append(' '.join(parts))

    # 第一列通常是科目名
    if col_text and (not NUM.search(col_text[0]) or len(col_text[0]) > 2):
        roles[0] = 'metric'

    year_cols = {}
    for c, txt in enumerate(col_text):
        if c == 0:
            continue
        m = YEAR_PAT.match(txt.strip())
        if m:
            year_cols[c] = int(m.group(1))

    # 优先用显式「本期/上期」标记
    explicit_cur = [c for c, t in enumerate(col_text) if c and CUR_PAT.search(t) and not CHG_PAT.search(t)]
    explicit_pri = [c for c, t in enumerate(col_text) if c and PRI_PAT.search(t) and not CHG_PAT.search(t)]
    change_cols = [c for c, t in enumerate(col_text) if c and CHG_PAT.search(t)]

    if explicit_cur and explicit_pri:
        roles[explicit_cur[0]] = 'current'
        roles[explicit_pri[0]] = 'prior'
        method = 'explicit_period_label'
    elif len(year_cols) >= 2:
        # 用年份推断：最大年份=本期，次大=上期
        ordered = sorted(year_cols.items(), key=lambda kv: -kv[1])
        roles[ordered[0][0]] = 'current'
        roles[ordered[1][0]] = 'prior'
        method = 'year_header_inference'
        # 三年及以上：第三列也可能是上期（跨年表），标注为待审计
        if len(ordered) >= 3:
            for c, _y in ordered[2:]:
                roles[c] = 'other_year'
            issues.append('multi_year_table_extra_columns')
    else:
        method = 'unresolved'
        issues.append('period_columns_not_recognized')

    for c in change_cols:
        if roles[c] == 'unknown':
            roles[c] = 'change'

    # 单位：优先表标题/表内文字，其次所在页
    unit = None
    m = UNIT_PAT.search(table_caption or '')
    if m:
        unit = m.group(2)
    if not unit:
        m = UNIT_PAT.search(page_text)
        if m:
            unit = m.group(2)
            issues.append('unit_from_page_not_table')

    # 口径
    scope_bits = []
    ctx = (table_caption or '') + ' ' + ' '.join(col_text[:1])
    if MERGE_PAT.search(ctx) or MERGE_PAT.search(page_text[:200]):
        scope_bits.append('合并')
    if PARENT_PAT.search(ctx) or PARENT_PAT.search(page_text[:200]):
        scope_bits.append('母公司')
    if SEG_PAT.search(ctx):
        scope_bits.append('分部')
    if not scope_bits:
        scope_bits.append('未标明')

    for c in range(ncols):
        detail[c] = {'header_text': col_text[c], 'role': roles[c]}

    return {
        'roles': roles,
        'method': method,
        'unit': unit,
        'scope': '/'.join(scope_bits),
        'issues': issues,
        'col_text': col_text,
        'year_cols': year_cols,
    }


def validate_three_values(cur, pri, chg):
    """三值自洽校验：(本期-上期)/|上期| ≈ 变动率。返回 (状态, 计算值)。"""
    if cur is None or pri is None:
        return 'missing_value', None
    if pri == 0:
        return 'prior_is_zero', None
    calc = round((cur - pri) / abs(pri) * 100, 2)
    if chg is None:
        return 'no_change_value', calc
    # 变动额列可能不是百分比
    if abs(chg) > 1000:
        return 'change_looks_like_amount', calc
    if abs(calc - chg) <= max(0.05, abs(chg) * 0.05):
        return 'consistent', calc
    return 'inconsistent', calc


def page_sentences(text):
    for s in re.split(r'[。！？；;\n]', text):
        s = re.sub(r'\s+', ' ', s).strip()
        if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
            yield s


def main() -> int:
    import pymupdf
    import pdfplumber

    rows = [json.loads(l) for l in (CORPUS / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    print(f'审计 {len(rows)} 份，开始 {time.strftime("%H:%M:%S")}', flush=True)

    candidates: list[dict] = []
    rejected: list[dict] = []
    per_company: list[dict] = []
    totals = Counter()
    col_methods = Counter()
    col_issues = Counter()
    consistency = Counter()

    for idx, rec in enumerate(rows, 1):
        pdf_path = CORPUS / rec['local_file']
        if not pdf_path.is_file():
            rejected.append({'company': rec['stock_code'], 'source_file': rec['local_file'],
                             'reason': 'file_missing'})
            continue

        doc = pymupdf.open(pdf_path)
        pages = [(i + 1, (p.get_text('text') or '')) for i, p in enumerate(doc)]
        doc.close()

        # ---- 第 1 项：与 manifest 一致性（代码/年份/公司名）----
        head = pages[0][1][:3000] if pages else ''
        all_text = ''.join(t for _, t in pages)
        meta_issues = []
        if rec['stock_code'] not in all_text:
            meta_issues.append('stock_code_not_found_in_document')
        if str(rec['report_year']) not in head:
            meta_issues.append('report_year_not_in_first_page')
        # 公司名核对：剥掉组织形式后缀后取前 4 字。
        # 旧版取前 2 字并在前 3 页里找，对「徐工机械」这类简称会产生大量误报。
        core = re.sub(r'(股份有限公司|有限责任公司|有限公司|集团|控股|科技|股份)', '',
                      rec['company_name']).strip()
        probe = core[:4] or rec['company_name'][:4]
        if probe not in all_text and rec['company_name'][:4] not in all_text:
            meta_issues.append('company_name_not_found_in_document')

        # 正文句子按页
        page_sents = {pno: list(page_sentences(txt)) for pno, txt in pages}
        all_sents = [(pno, s) for pno, ss in page_sents.items() for s in ss]

        # ---- 第 2 项：文字层 ----
        text_chars = sum(len(t.strip()) for _, t in pages)
        has_text = text_chars >= 2000
        body_pages_probed = min(len(pages), 20)
        body_text_chars = sum(len(t.strip()) for _, t in pages[:body_pages_probed])

        rec_stats = Counter()
        rec_stats['pages'] = len(pages)
        rec_stats['text_chars'] = text_chars

        # ---- 第 3/4/5/6 项 ----
        with pdfplumber.open(pdf_path) as pdf:
            for pno, page in enumerate(pdf.pages, 1):
                ptext = pages[pno - 1][1] if pno - 1 < len(pages) else ''
                for t in (page.extract_tables() or []):
                    if not t or len(t) < 3:
                        continue
                    header_rows = find_header_rows(t)
                    flat_head = ' '.join(c for row in header_rows for c in row)
                    if not (CUR_PAT.search(flat_head) or YEAR_PAT.search(flat_head)):
                        continue
                    if not (PRI_PAT.search(flat_head) or len(re.findall(r'20\d{2}', flat_head)) >= 2):
                        continue
                    rec_stats['tables_with_periods'] += 1
                    ncols = max(len(r) for r in t)
                    # 表标题：取表格上方最近的非空行
                    caption = ''
                    for line in reversed(ptext.splitlines()[-60:]):
                        if line.strip():
                            caption = line.strip()
                            break
                    info = classify_columns(header_rows, ncols, ptext, caption)
                    col_methods[info['method']] += 1
                    for iss in info['issues']:
                        col_issues[iss] += 1
                    if info['unit'] is None:
                        rec_stats['unit_unknown_tables'] += 1
                    if info['scope'] == '未标明':
                        rec_stats['scope_unknown_tables'] += 1

                    roles = info['roles']
                    cur_col = next((c for c, r in enumerate(roles) if r == 'current'), None)
                    pri_col = next((c for c, r in enumerate(roles) if r == 'prior'), None)
                    chg_col = next((c for c, r in enumerate(roles) if r == 'change'), None)
                    if cur_col is None or pri_col is None:
                        rec_stats['tables_unresolved'] += 1
                        continue

                    for row in t:
                        cells = [clean_cell(c) for c in row]
                        if not cells or not cells[0]:
                            continue
                        metric = cells[0]
                        if len(metric) > 30 or not any(k in metric for k in METRIC_HINTS):
                            continue
                        def at(c):
                            return cells[c] if c is not None and c < len(cells) else ''
                        cur, pri = canon(at(cur_col)), canon(at(pri_col))
                        chg_raw = at(chg_col)
                        chg = canon(chg_raw) if chg_raw else None
                        rec_stats['fact_rows'] += 1
                        if cur is None or pri is None:
                            rec_stats['rows_missing_value'] += 1

                        status, calc = validate_three_values(cur, pri, chg)
                        consistency[status] += 1

                        # ---- 第 5/6 项：正文锚点 ----
                        def anchor(value):
                            if value is None:
                                return None
                            hits = [(sp, s) for sp, s in all_sents
                                    if metric[:6] in s and any(canon(m.group(0)) == value
                                                               for m in NUM.finditer(s))]
                            return hits[0] if hits else None

                        cur_hit = anchor(cur)
                        pri_hit = anchor(pri)
                        growth_hit = None
                        for sp, s in all_sents:
                            if metric[:6] in s and re.search(r'同比|较上年|较上期|增减|变动|增长|下降', s) \
                                    and any(canon(m.group(0)) == cur for m in NUM.finditer(s)) and cur is not None:
                                growth_hit = (sp, s)
                                break

                        claim = None
                        claim_page = None
                        anchor_kind = 'table_only'
                        if cur_hit:
                            anchor_kind, claim, claim_page = 'current_anchor', cur_hit[1], cur_hit[0]
                        elif pri_hit:
                            anchor_kind, claim, claim_page = 'prior_anchor', pri_hit[1], pri_hit[0]
                        elif growth_hit:
                            anchor_kind, claim, claim_page = 'growth_sentence', growth_hit[1], growth_hit[0]

                        # 同义词只标记，不判等
                        syn_flag = None
                        for grp in SYNONYM_GROUPS:
                            if metric in grp:
                                others = [g for g in grp if g != metric]
                                if any(o in claim for o in others) if claim else False:
                                    syn_flag = 'synonym_needs_audit'
                                break

                        item = {
                            'company': rec['stock_code'],
                            'company_name': rec['company_name'],
                            'source_file': rec['local_file'],
                            'claim_text': claim,
                            'claim_page': claim_page,
                            'metric': metric,
                            'period': '本期' if anchor_kind == 'current_anchor' else
                                      ('上期' if anchor_kind == 'prior_anchor' else
                                       ('本期' if anchor_kind == 'growth_sentence' else None)),
                            'value': cur if anchor_kind in ('current_anchor', 'growth_sentence') else
                                     (pri if anchor_kind == 'prior_anchor' else None),
                            'unit': info['unit'],
                            'scope': info['scope'],
                            'fact_page': pno,
                            'fact_prior_value': pri,
                            'fact_change_value': chg,
                            'column_method': info['method'],
                            'three_value_check': status,
                            'three_value_calc': calc,
                            'anchor_kind': anchor_kind,
                            'label': None,
                            'audit_status': 'needs_human_review',
                        }
                        if syn_flag:
                            item['audit_status'] = syn_flag
                        if anchor_kind == 'table_only':
                            item['audit_status'] = 'table_only_no_claim'
                        if info['unit'] is None:
                            item['audit_status'] = 'unit_unknown'
                        if status == 'inconsistent':
                            item['audit_status'] = 'three_value_inconsistent'
                        if cur is None or pri is None:
                            item['audit_status'] = 'missing_value'
                        candidates.append(item)

        per_company.append({
            'company': rec['stock_code'],
            'company_name': rec['company_name'],
            'industry': rec['industry'],
            'source_file': rec['local_file'],
            'report_year': rec['report_year'],
            'report_version': rec['report_version'],
            'manifest_meta_issues': meta_issues,
            'pages': rec_stats['pages'],
            'text_chars': text_chars,
            'has_text_layer': has_text,
            'tables_with_periods': rec_stats['tables_with_periods'],
            'fact_rows': rec_stats['fact_rows'],
            'unit_unknown_tables': rec_stats['unit_unknown_tables'],
            'scope_unknown_tables': rec_stats['scope_unknown_tables'],
        })
        totals['pages'] += rec_stats['pages']
        totals['text_chars'] += text_chars
        totals['tables'] += rec_stats['tables_with_periods']
        totals['fact_rows'] += rec_stats['fact_rows']
        if idx % 5 == 0 or idx == len(rows):
            print(f'  [{idx}/{len(rows)}] {rec["stock_code"]} 完成 '
                  f'{time.strftime("%H:%M:%S")}', flush=True)

    # ---- 落盘 ----
    out_dir = CORPUS
    (out_dir / 'audit_candidates.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in candidates), encoding='utf-8')
    (out_dir / 'audit_rejected.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rejected), encoding='utf-8')
    (out_dir / 'audit_stats.json').write_text(json.dumps({
        'per_company': per_company,
        'totals': dict(totals),
        'column_methods': dict(col_methods),
        'column_issues': dict(col_issues),
        'three_value_consistency': dict(consistency),
        'anchor_kinds': dict(Counter(c['anchor_kind'] for c in candidates)),
        'audit_status': dict(Counter(c['audit_status'] for c in candidates)),
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'\n候选 {len(candidates)} 条，拒绝 {len(rejected)} 条')
    print(f'列识别方法：{dict(col_methods)}')
    print(f'锚点类型：{dict(Counter(c["anchor_kind"] for c in candidates))}')
    print(f'审计状态：{dict(Counter(c["audit_status"] for c in candidates))}')
    print(f'\n完成 {time.strftime("%H:%M:%S")}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
