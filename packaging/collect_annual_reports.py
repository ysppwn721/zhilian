"""采集中文年报：为轻量中文 reranker 提供「正文论断 — 表格事实元数据」训练素材。

严格遵循 答辩评测/给DeepSeek_300份年报采集与交接要求_20261002.md：
  - 排除冻结评测用到的 19 家公司（含其任何年份、修订版、节选）；
  - 每家公司最多取 N 份，避免像旧语料那样同一家堆多份；
  - 跳过摘要 / 英文版 / 扫描件（无文字层）；修订版单独标记、按 (公司,年份) 去重；
  - 清单字段与要求文档一致；
  - 原始 PDF 不进交付包（落在 output/，已被 .gitignore 覆盖）。

用法：
    python packaging/collect_annual_reports.py --target 50
    python packaging/collect_annual_reports.py --target 50 --out 答辩评测/annual_reports_new_20261002
"""
from __future__ import annotations

import argparse
import csv
import hashlib
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

# 冻结外部评测用到的公司：任何年份/修订版都不得进入本轮训练
FROZEN_CODES = {
    '000615', '000930', '002097', '002388', '002413', '002425', '002569', '002598',
    '002808', '002825', '300149', '300632', '600080', '600165', '600187', '603839',
    '688152', '836263', '873576',
}

# 检索计划 = (行业标签, 检索关键词)。同一行业可有多行（多轮关键词），
# 每行是独立配额单位——否则同名的第二组会被合并成一组，配额算重。
SEARCH_PLAN = [
    ('制造业', '智能制造'), ('制造业', '机械'), ('制造业', '装备制造'),
    ('软件与信息服务', '软件'), ('软件与信息服务', '信息技术'), ('软件与信息服务', '云计算'),
    ('消费与零售', '食品'), ('消费与零售', '零售'), ('消费与零售', '消费'),
    ('医药与生物', '医药'), ('医药与生物', '生物'), ('医药与生物', '制药'),
    ('能源与电力', '能源'), ('能源与电力', '电力'), ('能源与电力', '新能源'),
    ('材料与化工', '新材料'), ('材料与化工', '化工'), ('材料与化工', '材料'),
    ('建筑与地产', '建筑'), ('建筑与地产', '房地产'), ('建筑与地产', '工程'),
    ('交通与物流', '物流'), ('交通与物流', '运输'), ('交通与物流', '港口'),
    # ---- 第二轮：首轮实测「建筑与地产」「交通与物流」命中偏少（各 4 / 11 份），
    #      换更贴合的检索词补齐行业覆盖。----
    ('建筑与地产', '建设'), ('建筑与地产', '市政'), ('建筑与地产', '装饰'),
    ('建筑与地产', '钢结构'), ('建筑与地产', '园林'), ('建筑与地产', '工程咨询'),
    ('交通与物流', '航运'), ('交通与物流', '快递'), ('交通与物流', '供应链'),
    ('交通与物流', '铁路'), ('交通与物流', '航空'), ('交通与物流', '高速'),
    ('制造业', '电气'), ('制造业', '仪器'), ('制造业', '零部件'),
    ('制造业', '模具'), ('制造业', '轴承'), ('制造业', '焊接'),
    ('消费与零售', '乳业'), ('消费与零售', '饮料'), ('消费与零售', '纺织'),
    ('消费与零售', '服装'), ('消费与零售', '家居'), ('消费与零售', '商超'),
    ('医药与生物', '医疗器械'), ('医药与生物', '疫苗'), ('医药与生物', '中药'),
    ('医药与生物', '生物制品'), ('医药与生物', '诊断'),
    ('能源与电力', '燃气'), ('能源与电力', '光伏'), ('能源与电力', '风电'),
    ('能源与电力', '储能'), ('能源与电力', '热力'), ('能源与电力', '煤炭'),
    ('材料与化工', '涂料'), ('材料与化工', '塑料'), ('材料与化工', '橡胶'),
    ('材料与化工', '玻璃'), ('材料与化工', '陶瓷'),
    ('软件与信息服务', '大数据'), ('软件与信息服务', '网络安全'),
    ('软件与信息服务', '地理信息'), ('软件与信息服务', '工业软件'),
    ('软件与信息服务', '通信'),
]

# 行业清单（用于配额均分与统计）
INDUSTRIES = list(dict.fromkeys(name for name, _ in SEARCH_PLAN))

SKIP_TITLE = ('摘要', '英文', 'English', 'Annual Report', '公告', '问询', '反馈', '意见',
              '更正公告', '补充公告', '说明', '提示', '致歉')
VERSION_MARK = ('更正后', '更新后', '修订', '更正版')


def post(url, data, timeout=45):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def search_reports(keyword, se_date, pages=4, page_size=30):
    """按全文关键词检索年报，返回公告条目。"""
    out, seen = [], set()
    for page in range(1, pages + 1):
        params = {'pageNum': page, 'pageSize': page_size, 'column': 'szse',
                  'tabName': 'fulltext', 'category': 'category_ndbg_szsh',
                  'seDate': se_date, 'sortName': '', 'sortType': '', 'isHLtitle': 'true'}
        if keyword:
            params['searchkey'] = keyword
        try:
            data = post(QUERY, params)
        except Exception as exc:
            print(f'    [警告] 检索失败({keyword}): {exc}')
            break
        items = data.get('announcements') or []
        if not items:
            break
        for it in items:
            url = it.get('adjunctUrl') or ''
            if not url or url in seen:
                continue
            seen.add(url)
            title = re.sub(r'<[^>]+>', '', it.get('announcementTitle') or '').strip()
            # secName 里也可能带搜索高亮标签（如「徐工<em>机械</em>」），必须一并清掉，
            # 否则会写进清单和文件名。
            sec_name = re.sub(r'<[^>]*>', '', it.get('secName') or '').strip()
            out.append({
                'stock_code': (it.get('secCode') or '').strip(),
                'company_name': sec_name,
                'report_title': title,
                'pdf_url': STATIC + url,
                'announcement_time': it.get('announcementTime'),
                'kb': it.get('adjunctSize') or 0,
                'source_page_url': 'http://www.cninfo.com.cn/new/disclosure/stock?stockCode='
                                   + (it.get('secCode') or ''),
            })
        time.sleep(0.35)
    return out


def download(url, dest, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA,
                                                       'Referer': 'http://www.cninfo.com.cn/'})
            tmp = dest.with_suffix('.part')
            with urllib.request.urlopen(req, timeout=240) as resp, tmp.open('wb') as fh:
                total = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            tmp.replace(dest)
            return total
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    return 0


def has_text_layer(path, min_chars=2000, probe_pages=12):
    """扫描件判别：抽查前若干页文字量。返回 (是否有文字层, 抽样字符数)。"""
    import pymupdf
    doc = pymupdf.open(path)
    try:
        n = 0
        for i, page in enumerate(doc):
            if i >= probe_pages:
                break
            n += len((page.get_text('text') or '').strip())
        return n >= min_chars, n
    finally:
        doc.close()


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for c in iter(lambda: fh.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def year_of(title, fallback_time=None):
    m = re.search(r'(20\d{2})\s*年', title)
    if m:
        return int(m.group(1))
    if fallback_time:
        return time.gmtime(fallback_time / 1000).tm_year
    return None


def safe_name(code, year, version, name):
    clean = re.sub(r'[\\/:*?"<>|]', '', name)[:12]
    suffix = '_' + version if version else ''
    return f'{code}_{year or "未知"}{suffix}_{clean}.pdf'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=int, default=50, help='目标份数（默认 50）')
    ap.add_argument('--per-company', type=int, default=1, help='每家公司最多几份')
    ap.add_argument('--per-industry', type=int, default=0,
                    help='每个行业最多几份（0 = 按剩余名额自动均分，保证行业覆盖）')
    ap.add_argument('--out', type=Path, default=ROOT / '答辩评测' / 'annual_reports_new_20261002')
    ap.add_argument('--se-date', default='2024-04-01~2024-04-30',
                    help='单个披露时间窗')
    ap.add_argument('--se-dates', nargs='*', default=None,
                    help='多个披露时间窗（按顺序尝试，用于扩采；覆盖 --se-date）')
    ap.add_argument('--exclude-manifest', action='append', default=[],
                    help='已有批次清单路径；其中的公司代码将被排除（避免跨批次重复）')
    args = ap.parse_args()

    windows = args.se_dates or [args.se_date]
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / 'manifest.jsonl'
    done = {}
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec['pdf_url']] = rec
        print(f'已有清单 {len(done)} 条，续采')

    # 跨批次去重：已有批次里出现过的公司代码，本轮不再采
    exclude_codes = set()
    for rel in args.exclude_manifest:
        p = Path(rel)
        if not p.is_absolute():
            p = ROOT / rel
        if not p.is_file():
            print(f'  [警告] 排除清单不存在，忽略：{p}')
            continue
        codes = {json.loads(l)['stock_code'] for l in p.read_text(encoding='utf-8').splitlines() if l.strip()}
        exclude_codes |= codes
        print(f'  排除清单 {p.name}：{len(codes)} 家公司')
    if exclude_codes:
        print(f'  跨批次排除公司合计 {len(exclude_codes)} 家')

    # 行业配额：不设限时，第一个行业会把名额吃光（实测软件业一家占 33/50）。
    # 默认按剩余名额在尚未起步的行业间均分，保证覆盖。
    remaining = max(0, args.target - len(done))
    started = {r['industry'] for r in done.values()}
    pending_industries = [name for name in INDUSTRIES if name not in started]
    if args.per_industry > 0:
        quota = args.per_industry
    elif pending_industries:
        quota = max(1, -(-remaining // len(pending_industries)))
    else:
        quota = remaining
    print(f'目标 {args.target} 份 · 每家上限 {args.per_company} 份 · 每行业上限 {quota} 份 · '
          f'排除冻结公司 {len(FROZEN_CODES)} 家\n')

    per_company: dict[str, int] = {}
    for rec in done.values():
        per_company[rec['stock_code']] = per_company.get(rec['stock_code'], 0) + 1
    seen_company_year: set[tuple[str, int | None]] = {
        (r['stock_code'], r.get('report_year')) for r in done.values()}
    per_industry: dict[str, int] = {}
    for rec in done.values():
        per_industry[rec['industry']] = per_industry.get(rec['industry'], 0) + 1

    collected = len(done)
    skipped = []

    for industry, keyword in SEARCH_PLAN:
        if collected >= args.target:
            break
        if per_industry.get(industry, 0) >= quota:
            continue
        # 每个关键词独立配额单位；逐个时间窗尝试。
        # 单月窗口公告量有限（实测约 180 条），扩采必须换窗口。
        for window in windows:
            if collected >= args.target or per_industry.get(industry, 0) >= quota:
                break
            print(f'--- 行业「{industry}」关键词「{keyword}」窗口 {window} ---')
            try:
                items = search_reports(keyword, window)
            except Exception as exc:
                print(f'    检索异常: {exc}')
                continue
            print(f'    命中 {len(items)} 条')
            new_in_window = 0
            for it in items:
                if collected >= args.target:
                    break
                if per_industry.get(industry, 0) >= quota:
                    break
                code = it['stock_code']
                if not code or code in FROZEN_CODES or code in exclude_codes:
                    continue
                if per_company.get(code, 0) >= args.per_company:
                    continue
                if any(t in it['report_title'] for t in SKIP_TITLE):
                    skipped.append({**it, 'reason': '标题命中跳过词（摘要/英文/公告类）'})
                    continue
                if not (800 <= it['kb'] <= 20000):
                    skipped.append({**it, 'reason': f"体积 {it['kb']}KB 不在 800-20000KB"})
                    continue
                version = '修订版' if any(v in it['report_title'] for v in VERSION_MARK) else '原版'
                year = year_of(it['report_title'], it.get('announcement_time'))
                key = (code, year)
                if key in seen_company_year:
                    skipped.append({**it, 'reason': f'同公司同年份已采（{version}）'})
                    continue

                dest = args.out / safe_name(code, year, version if version == '修订版' else '', it['company_name'])
                if not dest.exists():
                    try:
                        size = download(it['pdf_url'], dest)
                    except Exception as exc:
                        skipped.append({**it, 'reason': f'下载失败 {type(exc).__name__}'})
                        print(f'    ✗ {code} 下载失败: {exc}')
                        continue
                    time.sleep(0.5)
                else:
                    size = dest.stat().st_size
                try:
                    ok, chars = has_text_layer(dest)
                except Exception as exc:
                    ok, chars = False, 0
                    skipped.append({**it, 'reason': f'PDF 读取失败 {type(exc).__name__}'})
                if not ok:
                    skipped.append({**it, 'reason': f'疑似扫描件（抽样仅 {chars} 字符）'})
                    print(f'    ✗ {code} {it["company_name"][:10]} 无文字层，删除')
                    dest.unlink(missing_ok=True)
                    continue

                rec = {
                    'stock_code': code,
                    'company_name': it['company_name'],
                    'industry': industry,
                    'industry_keyword': keyword,
                    'disclosure_window': window,
                    'report_year': year,
                    'publication_date': time.strftime(
                        '%Y-%m-%d', time.gmtime(it['announcement_time'] / 1000)) if it.get('announcement_time') else '',
                    'report_title': it['report_title'],
                    'report_version': version,
                    'source_page_url': it['source_page_url'],
                    'pdf_url': it['pdf_url'],
                    'local_file': dest.name,
                    'sha256': sha256(dest),
                    'bytes': size,
                    'language': 'zh',
                    'has_text_layer': True,
                    'text_probe_chars': chars,
                    'source_terms_note': '巨潮资讯网公开披露；许可状态待核对，原始 PDF 不进交付包',
                }
                with manifest_path.open('a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                done[it['pdf_url']] = rec
                per_company[code] = per_company.get(code, 0) + 1
                per_industry[industry] = per_industry.get(industry, 0) + 1
                seen_company_year.add(key)
                collected += 1
                new_in_window += 1
                print(f'    ✓ [{collected}/{args.target}] {code} {it["company_name"][:12]:12} '
                      f'{year} {version} {size/1024/1024:.2f}MB 文字{chars}字符')
            if new_in_window == 0:
                print(f'    该窗口无新增（公司已采或已排除），换下一个窗口')

    # 行业分布统计
    rows = list(done.values())
    print(f'\n{"="*70}')
    print(f'实际采集 {len(rows)} 份')
    from collections import Counter
    print('行业分布:')
    for k, v in Counter(r['industry'] for r in rows).most_common():
        print(f'   {k:16} {v:>3} 份')
    print(f'独立公司数: {len({r["stock_code"] for r in rows})}')
    print(f'修订版: {sum(1 for r in rows if r["report_version"] == "修订版")} 份')
    print(f'年份分布: {dict(sorted(Counter(r["report_year"] for r in rows).items()))}')
    print(f'{"="*70}')
    print(f'清单: {manifest_path}')

    if skipped:
        skip_path = args.out / 'skipped.jsonl'
        skip_path.write_text('\n'.join(json.dumps(s, ensure_ascii=False) for s in skipped),
                             encoding='utf-8')
        print(f'跳过记录 {len(skipped)} 条 → {skip_path.name}')

    # CSV 版清单，便于人工核对
    csv_path = args.out / 'manifest.csv'
    if rows:
        with csv_path.open('w', encoding='utf-8-sig', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'CSV 清单 → {csv_path.name}')
    print('\n提醒：原始 PDF 仅用于本地实验，不进入交付包；所有自动配对均为弱标注。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
