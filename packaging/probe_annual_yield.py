"""实测：新抓一份年报 → 表格解析的原始产能（**不是**可用训练样本数）。

⚠️ 重要更正（2026-10-02 晚，经 50 份独立审计后修订）
----------------------------------------------------
本脚本的「数值可对上」判据是**在整份文档文本里找数值字面**，衡量的是
"程序化标注上限"，**不是可用训练样本数**。两者差距极大：

    整份文档数值命中率   96–98%   ← 本脚本测的是这个（会被误读为"产能很高"）
    句子级正文锚点率      约 1%    ← 真正决定可用样本数的是这个

对 50 份年报的独立审计（`audit_annual_corpus.py`）实测：8214 条自动抽取候选中，
只有 97 条能定位到正文句子，其中仅 73 条三值可核。原因是每份年报约 159 个
「(指标, 本期值, 上期值)」三值组里，绝大多数是"按组合计提坏账准备"这类
**只在表格里出现的明细行**，正文从不提及。

**估算训练数据量请用 `probe_prior_anchor.py` 的句子级判据。**
保留本脚本是因为它仍能度量表格解析的原始产能与失败形态。

用法：
    python packaging/probe_annual_yield.py --limit 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
QUERY = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
STATIC = 'http://static.cninfo.com.cn/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'

NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
METRIC_HINTS = ('收入', '成本', '费用', '利润', '资产', '负债', '现金', '合计', '总额', '余额')


def post(url, data, timeout=40):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def list_reports(pages=3):
    out, seen = [], set()
    for page in range(1, pages + 1):
        try:
            data = post(QUERY, {'pageNum': page, 'pageSize': 30, 'column': 'szse',
                                'tabName': 'fulltext', 'category': 'category_ndbg_szsh',
                                'seDate': '2024-04-01~2024-04-30', 'sortName': '', 'sortType': '',
                                'isHLtitle': 'true'})
        except Exception as exc:
            print(f'  [警告] 查询失败: {exc}')
            continue
        for it in data.get('announcements') or []:
            url = it.get('adjunctUrl') or ''
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({'code': it.get('secCode') or '', 'name': it.get('secName') or '',
                        'url': url, 'kb': it.get('adjunctSize') or 0,
                        'title': re.sub(r'<[^>]+>', '', it.get('announcementTitle') or '')})
        time.sleep(0.4)
    return out


def download(url, dest):
    req = urllib.request.Request(STATIC + url, headers={'User-Agent': UA,
                                                        'Referer': 'http://www.cninfo.com.cn/'})
    tmp = dest.with_suffix('.part')
    with urllib.request.urlopen(req, timeout=180) as resp, tmp.open('wb') as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)
    return dest


def sentences(path):
    import pymupdf
    doc = pymupdf.open(path)
    out = []
    for page_no, page in enumerate(doc, 1):
        text = page.get_text('text') or ''
        for s in re.split(r'[。！？；;\n]', text):
            s = re.sub(r'\s+', ' ', s).strip()
            if 8 <= len(s) <= 240 and any('\u4e00' <= c <= '\u9fff' for c in s):
                out.append((page_no, s))
    doc.close()
    return out


def num_tokens(text):
    return {m.group(0).replace(',', '').replace('−', '-') for m in NUM.finditer(text)}


def fact_tables(path):
    """抽出候选财务表：找同时含本期/上期期间列的表格。"""
    import pdfplumber
    tables = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            for t in (page.extract_tables() or []):
                if not t or len(t) < 3:
                    continue
                head = ' '.join(str(c or '') for row in t[:3] for c in row)
                has_cur = re.search(r'本期|本报告期|期末|本年度', head)
                has_pri = re.search(r'上期|上年同期|上年|期初|上年度', head)
                if has_cur and has_pri:
                    rows = []
                    for row in t:
                        cells = [re.sub(r'\s+', '', str(c or '')) for c in row]
                        if not cells:
                            continue
                        label = cells[0]
                        if not label or len(label) > 30:
                            continue
                        if not any(k in label for k in METRIC_HINTS):
                            continue
                        nums = [c for c in cells[1:] if NUM.fullmatch(c or '')]
                        if len(nums) >= 2:
                            rows.append({'label': label, 'nums': nums[:2]})
                    if rows:
                        tables.append({'page': page_no, 'rows': rows})
    return tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=3, help='实测多少份')
    ap.add_argument('--out', type=Path, default=ROOT / 'output' / 'annual_yield_probe')
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    frozen_path = ROOT / '答辩评测' / 'external_programmatic_holdout_all.jsonl'
    frozen_codes = set()
    if frozen_path.is_file():
        for line in frozen_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            m = re.search(r'(\d{6})', json.loads(line).get('group_id', ''))
            if m:
                frozen_codes.add(m.group(1))
    print(f'冻结集公司 {len(frozen_codes)} 家，将全部排除\n')

    listed = list_reports(pages=6)
    print(f'检索到 {len(listed)} 条公告记录')
    picked, seen_codes = [], set()
    for it in listed:
        if it['code'] in frozen_codes or it['code'] in seen_codes:
            continue
        if not (800 <= it['kb'] <= 12000):
            continue
        if '摘要' in it['title'] or '英文' in it['title'] or 'Annual' in it['title']:
            continue
        seen_codes.add(it['code'])
        picked.append(it)
        if len(picked) >= args.limit:
            break

    print(f'选定 {len(picked)} 份（全部为冻结集外公司、非摘要、800-12000KB）\n')
    total_sents = total_tables = total_rows = total_matched = 0
    per_report = []
    for i, it in enumerate(picked, 1):
        dest = args.out / f"{it['code']}_{it['name'][:12]}.pdf".replace('*', '')
        if not dest.exists():
            try:
                download(it['url'], dest)
            except Exception as exc:
                print(f'  [{i}] 下载失败 {it["code"]}: {exc}')
                continue
            time.sleep(0.6)
        sents = sentences(dest)
        tables = fact_tables(dest)
        rows = sum(len(t['rows']) for t in tables)
        # 「可验证」判据：表格数值能在某句正文中以字面形式找到
        table_nums = {n for t in tables for r in t['rows'] for n in r['nums']}
        sents_tok = [(p, s, num_tokens(s)) for p, s in sents]
        matched = 0
        for n in table_nums:
            key = n.replace('.0', '')
            if any(key in tok or n in tok for _, _, tok in sents_tok):
                matched += 1
        per_report.append({'code': it['code'], 'name': it['name'],
                           'mb': round(dest.stat().st_size / 1024 / 1024, 2),
                           'sentences': len(sents), 'tables': len(tables),
                           'fact_rows': rows, 'numeric_matched': matched})
        total_sents += len(sents)
        total_tables += len(tables)
        total_rows += rows
        total_matched += matched
        print(f'  [{i}/{len(picked)}] {it["code"]} {it["name"][:14]:14} '
              f'句子 {len(sents):>5} · 财务表 {len(tables):>3} · 事实行 {rows:>4} · 数值可对上 {matched:>4}')

    n = len(per_report) or 1
    print(f'\n{"="*66}')
    print(f'实测 {len(per_report)} 份，平均：')
    print(f'  正文句子      {total_sents/n:>8.0f} 句/份')
    print(f'  候选财务表    {total_tables/n:>8.1f} 张/份')
    print(f'  事实行        {total_rows/n:>8.1f} 行/份')
    print(f'  数值可对上    {total_matched/n:>8.1f} 条/份   ← 可构造标签的上限')
    print(f'{"="*66}')
    print(f'\n换算：')
    for target in (2000, 5000, 10000):
        print(f'  要拿到 {target:>6} 条可验证事实 → 约需 {target/(total_matched/n):>6.0f} 份年报 '
              f'（约 {target/(total_matched/n)*3.0/1024:>5.1f} GB）')

    (args.out / 'yield_probe.json').write_text(
        json.dumps({'per_report': per_report, 'frozen_companies': sorted(frozen_codes)},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n明细已写入 {args.out / "yield_probe.json"}')


if __name__ == '__main__':
    main()
