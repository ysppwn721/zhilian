"""生成口径判定导航包：每组给出页码、表格原始表头与数据行、口径线索词。

目的：让「打开 PDF 找表」变成「照着页码核对」。多数组光看导出的表头与数据行
就能判定口径，不必逐份翻年报。

产出：
  scope_navigation.md    给人看的导航（按台账组编号）
  scope_navigation.csv   同内容，便于边填边对照
"""
from __future__ import annotations

import argparse
import csv
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

# 口径线索词：在表格上方的页面文本里找，用于提示人工判断
SCOPE_HINT = re.compile(r'合并|母公司|公司本部|分部|分产品|分行业|分地区|业务板块|'
                        r'主要控股参股公司|子公司')
UNIT_LINE = re.compile(r'单位\s*[:：]\s*(人民币)?\s*(元|万元|亿元|千元)')


def clean(c):
    return re.sub(r'\s+', '', str(c or ''))


def norm_caption(c: str) -> str:
    c = (c or '').strip()
    return c[:60] if len(c) >= 4 else '（空/无效 caption）'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_merged')
    ap.add_argument('--ledger', type=Path, default=None)
    args = ap.parse_args()

    d = args.corpus
    ledger_path = args.ledger or (d / 'adjudication_ledger.csv')
    with ledger_path.open(encoding='utf-8-sig', newline='') as fh:
        ledger = list(csv.DictReader(fh))
    captions = [norm_caption(r['表格标题(caption)']) for r in ledger]
    # caption -> 组编号
    group_of = {}
    for r in ledger:
        group_of[norm_caption(r['表格标题(caption)'])] = int(r['序号'])

    cs = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]

    by_group: dict[str, list[dict]] = defaultdict(list)
    for c in usable:
        key = norm_caption(c.get('caption'))
        if key in group_of:
            by_group[key].append(c)

    import pymupdf
    import pdfplumber

    # 按 (公司, 页) 缓存表头与线索，避免重复打开 PDF
    cache: dict[tuple[str, int], dict] = {}

    def probe(company: str, filename: str, page_no: int) -> dict:
        key = (company, page_no)
        if key in cache:
            return cache[key]
        info = {'unit_line': '', 'hints': [], 'header': [], 'rows': [], 'page_head': ''}
        p = d / filename
        if not p.is_file():
            cache[key] = info
            return info
        doc = pymupdf.open(p)
        try:
            if 1 <= page_no <= len(doc):
                text = doc[page_no - 1].get_text('text') or ''
                info['page_head'] = re.sub(r'\s+', ' ', text[:220]).strip()
                m = UNIT_LINE.search(text)
                if m:
                    info['unit_line'] = m.group(0)
                info['hints'] = sorted(set(SCOPE_HINT.findall(text)))
        finally:
            doc.close()
        with pdfplumber.open(p) as pdf:
            if 1 <= page_no <= len(pdf.pages):
                page = pdf.pages[page_no - 1]
                tabs = page.extract_tables() or []
                if tabs:
                    t = max(tabs, key=lambda x: len(x) * max((len(r) for r in x), default=0))
                    for row in t[:5]:
                        cells = [clean(c)[:22] for c in row[:7]]
                        info['rows'].append(cells)
                    info['header'] = info['rows'][0] if info['rows'] else []
        cache[key] = info
        return info

    L = []
    A = L.append
    A('# 口径判定导航包')
    A('')
    A('每组列出：涉及公司与文件、页码、表格原始表头与数据行、单位行、页面口径线索词。')
    A('')
    A('**判定口径只需看两类证据：**')
    A('')
    A('1. 表头上方或页面里是否出现「母公司 / 公司本部」（→ 母公司口径）')
    A('2. 是否按「分产品 / 分行业 / 分地区 / 业务板块」拆行（→ 分部口径）')
    A('')
    A('两者都没有，且位于「主要会计数据」「管理层讨论与分析」章节 → 一般取**合并**口径。')
    A('')
    A('> 注意：**不要**把「未出现合并二字」当成无法判定。年报的合并数表通常不写"合并"，')
    A('> 这是格式惯例；但母公司口径的表**一定会**写"母公司"，这是可用的判别点。')
    A('')

    csv_rows = []
    # 关键：不能只按 caption 分组。实测同一 caption（如「利润表及现金流量表相关科目变动分析表」）
    # 在不同公司/页面上，页面口径线索不同（有的页出现「母公司」，有的只出现「子公司」）。
    # 因此细化到 (caption, 线索状态, 单位行) —— 这才是真正同质的判定单位。
    for cap in sorted(by_group, key=lambda k: group_of[k]):
        items = by_group[cap]
        gno = group_of[cap]
        sub: dict[tuple, list[dict]] = defaultdict(list)
        probe_cache: dict[int, dict] = {}
        for c in items:
            info = probe(c['company'], c['source_file'], c['fact_page'] or 1)
            probe_cache[id(c)] = info
            hint_key = '/'.join(info['hints']) or '（无口径词）'
            sub[(hint_key, info['unit_line'] or '未声明')].append(c)

        A(f'## 第 {gno} 组 · {len(items)} 条 · {len({c["company"] for c in items})} 家公司')
        A('')
        A(f'**表格标题（caption）**：`{cap}`')
        A('')
        A(f"**单位候选**：{dict(Counter(c.get('unit') or '空' for c in items))}")
        A('')
        if len(sub) > 1:
            A(f'> ⚠ 本组的页面线索不一致，已拆成 **{len(sub)} 个判定单元**——'
              f'同一张表在不同公司/页面的口径线索不同，请分别判定。')
            A('')

        for (hint_key, unit_line), group_items in sorted(sub.items(), key=lambda kv: -len(kv[1])):
            A(f'### 判定单元 {gno}.{sorted(sub).index(((hint_key, unit_line)))+1}'
              f' · {len(group_items)} 条 · {len({c["company"] for c in group_items})} 家公司')
            A('')
            A(f'**该单元口径线索**：`{hint_key}` · **单位行**：`{unit_line}`')
            A('')
            A('| 公司 | 文件 | 表页 | 正文页 |')
            A('|---|---|---:|---:|')
            shown = 0
            for c in group_items:
                A(f"| {c['company']} | `{c['source_file'][:30]}` | {c['fact_page']} | {c['claim_page']} |")
                shown += 1
                if shown >= 6 and len(group_items) > 8:
                    A(f"| … | 其余 {len(group_items)-shown} 条，页码见 `candidates_v2.csv` | | |")
                    break
            A('')
            sample = group_items[0]
            info = probe_cache[id(sample)]
            if info['rows']:
                A(f"**表格样例（{sample['company']} 第 {sample['fact_page']} 页）**：")
                A('')
                A('```')
                for row in info['rows']:
                    A(' | '.join(row))
                A('```')
                A('')
            if info['page_head']:
                A(f"**该页开头**：{info['page_head'][:150]}")
                A('')
            A('**判定**：☐ 合并  ☐ 母公司  ☐ 分部  ☐ 排除　　确认单位：______　　判定人：______')
            A('')
            csv_rows.append({
                '组号': gno,
                '判定单元': f'{gno}.{sorted(sub).index(((hint_key, unit_line)))+1}',
                '表格标题': cap,
                '口径线索词': hint_key,
                '该页单位行': unit_line,
                '条数': len(group_items),
                '公司数': len({c['company'] for c in group_items}),
                '样例公司': sample['company'],
                '样例文件': sample['source_file'],
                '样例表页': sample['fact_page'],
                '样例正文页': sample['claim_page'],
                '表头样例': ' | '.join(info['header']),
                '口径判定(人工填)': '',
                '确认单位(人工填)': '',
                '判定人': '',
                '备注': '',
            })
        A('---')
        A('')

    (d / 'scope_navigation.md').write_text('\n'.join(L), encoding='utf-8')
    with (d / 'scope_navigation.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
        w.writeheader()
        w.writerows(csv_rows)

    print(f'导航包 → scope_navigation.md（{len(csv_rows)} 组）')
    print(f'        → scope_navigation.csv')
    print()
    # 提示哪些组有明显口径线索
    for r in csv_rows:
        mark = '★' if r['口径线索词'] not in ('', '—') else ' '
        print(f"  {mark} 第{r['组号']:>2}组 {r['条数']:>3}条 · "
              f"线索 [{r['口径线索词'][:20]}] · {r['表格标题'][:38]}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
