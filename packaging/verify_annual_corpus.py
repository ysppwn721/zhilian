"""独立验收已采集的年报语料：不信任采集器的自我报告，重新核对每一条。

检查项：
  1. 冻结公司零污染（19 家，含任意年份/修订版）
  2. 清单字段完整性（与要求文档的字段表逐项比对）
  3. 本地文件存在、大小与清单一致、SHA256 与清单一致
  4. 公司名无 HTML 残渣
  5. 公司不重复、行业分布合理
  6. 每份确实有文字层（重新抽样，不复用采集时的结论）
  7. 配对有效率试算：表格三值组能否在正文中找到锚点
"""
from __future__ import annotations

import hashlib
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
FROZEN = {'000615', '000930', '002097', '002388', '002413', '002425', '002569', '002598',
          '002808', '002825', '300149', '300632', '600080', '600165', '600187', '603839',
          '688152', '836263', '873576'}
REQUIRED_FIELDS = ['stock_code', 'company_name', 'industry', 'report_year', 'publication_date',
                   'report_title', 'report_version', 'source_page_url', 'pdf_url', 'local_file',
                   'sha256', 'bytes', 'language', 'has_text_layer', 'source_terms_note']
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
METRIC_HINTS = ('收入', '成本', '费用', '利润', '资产', '负债', '现金', '合计', '总额', '余额', '每股')


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for c in iter(lambda: fh.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def canon(x):
    x = x.replace(',', '').replace('−', '-').replace('(', '-').replace(')', '')
    try:
        return round(float(x.rstrip('%')), 2)
    except ValueError:
        return None


def main() -> int:
    ap_dir = sys.argv[1] if len(sys.argv) > 1 else '答辩评测/annual_reports_new_20261002'
    d = ROOT / ap_dir
    mf = d / 'manifest.jsonl'
    if not mf.is_file():
        print(f'✗ 找不到清单 {mf}')
        return 2
    rows = [json.loads(l) for l in mf.read_text(encoding='utf-8').splitlines() if l.strip()]
    fails: list[str] = []

    print(f'=== 验收 {ap_dir} ===')
    print(f'清单 {len(rows)} 条\n')

    # 1) 冻结公司
    hit = sorted({r['stock_code'] for r in rows} & FROZEN)
    print(f'[1] 冻结公司污染         : {"✓ 无" if not hit else "✗ " + str(hit)}')
    if hit:
        fails.append('frozen_company_leak')

    # 2) 字段完整性
    missing = {f for r in rows for f in REQUIRED_FIELDS if f not in r or r[f] in ('', None)}
    print(f'[2] 必需字段缺失         : {"✓ 无" if not missing else "✗ " + str(sorted(missing))}')
    if missing:
        fails.append('missing_fields')

    # 3) 文件与哈希
    bad_file, bad_hash, bad_size = [], [], []
    for r in rows:
        p = d / r['local_file']
        if not p.is_file():
            bad_file.append(r['local_file'])
            continue
        if p.stat().st_size != r['bytes']:
            bad_size.append(r['local_file'])
        if sha256(p) != r['sha256']:
            bad_hash.append(r['local_file'])
    print(f'[3] 文件存在/大小/哈希   : '
          f'{"✓ 全部一致" if not (bad_file or bad_hash or bad_size) else f"✗ 缺{len(bad_file)} 大小不符{len(bad_size)} 哈希不符{len(bad_hash)}"}')
    if bad_file or bad_hash or bad_size:
        fails.append('file_integrity')

    # 4) HTML 残渣
    junk = [r['company_name'] for r in rows if '<' in r['company_name'] or '>' in r['company_name']]
    print(f'[4] 公司名 HTML 残渣     : {"✓ 无" if not junk else "✗ " + str(junk[:5])}')
    if junk:
        fails.append('html_residue')

    # 5) 公司重复与分布
    dup = [c for c, n in Counter(r['stock_code'] for r in rows).items() if n > 1]
    print(f'[5] 公司重复             : {"✓ 无" if not dup else "✗ " + str(dup)}')
    print(f'    独立公司             : {len({r["stock_code"] for r in rows})}')
    print(f'    行业分布             : {dict(Counter(r["industry"] for r in rows).most_common())}')
    print(f'    年份分布             : {dict(sorted(Counter(r["report_year"] for r in rows).items()))}')
    if dup:
        fails.append('duplicate_company')

    # 6) 重新抽样文字层
    import pymupdf
    no_text = []
    total_chars = 0
    for r in rows:
        p = d / r['local_file']
        if not p.is_file():
            continue
        doc = pymupdf.open(p)
        n = sum(len((pg.get_text('text') or '').strip()) for i, pg in enumerate(doc) if i < 12)
        doc.close()
        total_chars += n
        if n < 2000:
            no_text.append((r['stock_code'], n))
    print(f'[6] 重新抽检文字层       : {"✓ 全部有文字层" if not no_text else "✗ " + str(no_text)}')
    print(f'    抽样字符合计         : {total_chars:,}')
    if no_text:
        fails.append('no_text_layer')

    # 7) 配对有效率试算
    print(f'\n[7] 配对有效率试算（逐份算「本期值可在正文找到」的三值组）')
    per = []
    for r in rows:
        p = d / r['local_file']
        if not p.is_file():
            continue
        doc = pymupdf.open(p)
        text = ''.join((pg.get_text('text') or '') for pg in doc)
        doc.close()
        sset = {canon(m.group(0)) for m in NUM.finditer(text)} - {None}
        import pdfplumber
        triples = []
        with pdfplumber.open(p) as pdf:
            for page in pdf.pages:
                for t in (page.extract_tables() or []):
                    head = ' '.join(str(c or '') for row in t[:3] for c in row)
                    if not (re.search(r'本期|本报告期|期末|本年度', head)
                            and re.search(r'上期|上年同期|上年|期初|上年度', head)):
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
        grounded = [t for t in triples if t[1] in sset]
        per.append((r['stock_code'], len(triples), len(grounded)))
    if per:
        tot_t = sum(x[1] for x in per)
        tot_g = sum(x[2] for x in per)
        zero = [c for c, t, g in per if g == 0]
        print(f'    合计三值组 {tot_t}，本期值可锚定 {tot_g}  '
              f'（锚定率 {tot_g/max(1,tot_t)*100:.1f}%）')
        print(f'    平均每份可锚定 {tot_g/len(per):.0f} 组')
        print(f'    零锚定的报告 {len(zero)} 份: {zero[:8]}')
        print(f'    按此换算：2000 组需 {2000/max(1,tot_g/len(per)):.0f} 份，'
              f'5000 组需 {5000/max(1,tot_g/len(per)):.0f} 份')

    print(f'\n{"="*62}')
    if fails:
        print(f'✗ 验收未通过：{fails}')
        return 1
    print('✓ 七项全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
