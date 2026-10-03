"""第 1 步：把口径判定从「逐条 140 次」压到「按表格标题 15 次」。

为什么可以这样压
----------------
实测 140 条候选的 caption 去重后只有 15 种。同一张表出来的记录，
口径必然相同——所以人工只需要对**每个 caption 判定一次**，
而不是对每条候选判定一次。

判定输出
--------
  adjudication_ledger.csv   每个 caption 一行，含：出现次数、样例、判定列
  adjudication_ledger.md    同一内容的可读版，附判定规则与注意事项

人工填完 CSV 的「口径判定」列后，由 apply_adjudication.py 回填到候选记录。
"""
from __future__ import annotations

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

# 自动预判：仅用于给人工一个起点，**不作为最终结论**。
# 依据是中国证监会年报格式准则的章节惯例，不是从文本推断——所以必须人工确认。
PREJUDGE = [
    (r'主要会计数据|主要财务指标|前三年主要会计数据', '合并（惯例）',
     '年报第二节"主要会计数据/主要财务指标"按规定为合并报表口径'),
    (r'利润表及现金流量表相关科目变动分析表', '合并（惯例）',
     '管理层讨论与分析中的科目变动分析表，默认取合并数（有母公司口径时会另行列表）'),
    (r'收入和成本分析|主营业务分析|营业收入扣除情况表|收入和成本', '合并（惯例）',
     '管理层讨论与分析的收入/成本分析，默认合并数'),
    (r'研发投入情况|研发人员情况|销售费用构成', '合并（惯例）',
     '费用/投入类明细表，默认合并数'),
    (r'分行业|分产品|分地区|分销售模式|主营业务分|电量、收入及成本', '分部',
     '按业务/产品/地区/主体拆分，属分部口径'),
    (r'母公司', '母公司', '标题含"母公司"'),
    (r'^合并', '合并', '标题含"合并"'),
    (r'^单位[:：]', '待定', 'caption 只抓到单位行，未拿到表名，必须人工看该表'),
    (r'主要生产经营信息|资产及负债状况', '待定',
     '需打开 PDF 判断该表是合并数还是母公司数/分部数'),
]


def prejudge(caption: str) -> tuple[str, str]:
    for pat, verdict, why in PREJUDGE:
        if re.search(pat, caption):
            return verdict, why
    return '待定', '无法从标题判断'


def main() -> int:
    ap = ROOT / '答辩评测' / 'annual_reports_merged'
    cs = [json.loads(l) for l in (ap / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]

    groups = defaultdict(list)
    for c in usable:
        cap = (c.get('caption') or '').strip()
        key = cap[:60] if cap and len(cap) >= 4 else '（空/无效 caption）'
        groups[key].append(c)

    rows = []
    for i, (cap, items) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
        verdict, why = prejudge(cap)
        units = Counter(c['unit'] for c in items)
        scopes = Counter(c['scope'] for c in items)
        rows.append({
            '序号': i,
            '表格标题(caption)': cap,
            '条数': len(items),
            '涉及公司数': len({c['company'] for c in items}),
            '单位分布': '; '.join(f'{k or "空"}×{v}' for k, v in units.most_common()),
            '当前口径标记': '; '.join(f'{k}×{v}' for k, v in scopes.most_common()),
            '自动预判': verdict,
            '预判依据': why,
            '口径判定(人工填)': '',
            '确认单位(人工填)': '',
            '判定人': '',
            '判定时间': '',
            '备注': '',
        })

    csv_path = ap / 'adjudication_ledger.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    L = []
    A = L.append
    A('# 口径判定台账（第 1 步）')
    A('')
    A(f'共 **{len(rows)} 组**，覆盖 **{len(usable)} 条**候选。')
    A('')
    A('## 为什么只需要判定这么少次')
    A('')
    A('同一张表出来的记录口径必然相同，而 140 条候选的表格标题去重后只有 '
      f'{len(rows)} 种。所以人工判定 **{len(rows)} 次**即可覆盖全部候选，'
      '不需要逐条看。')
    A('')
    A('## 判定前必读')
    A('')
    A('1. **"合并（惯例）"是预判，不是结论。** 它依据《公开发行证券的公司信息披露内容与格式准则'
      '第 2 号》对年报第二节"主要会计数据"的规定，但**必须打开 PDF 对应页确认**。')
    A('2. **本项目不能靠文本推断口径。** 年报的"主要会计数据"表通常不写"合并"二字，'
      '若按词面判断会得出"未标明"——而口径错了会导致把合并数与母公司数配成一对，')
    A('   数值看起来自洽但语义错误。这是当前最大的静默错误来源。')
    A('3. **单位必须同时确认。** 元与万元相差 10⁴ 倍；若 caption 只抓到"单位：元"，')
    A('   仍需确认该表的数值是否真的以元为单位（有些表在标题处写万元）。')
    A('')
    A('## 台账')
    A('')
    A('| # | 表格标题 | 条数 | 公司数 | 单位 | 自动预判 | 依据 |')
    A('|---:|---|---:|---:|---|---|---|')
    for r in rows:
        A(f"| {r['序号']} | {r['表格标题(caption)'][:44]} | {r['条数']} | {r['涉及公司数']} | "
          f"{r['单位分布'][:22]} | **{r['自动预判']}** | {r['预判依据'][:34]} |")
    A('')
    A('## 填完后')
    A('')
    A('```powershell')
    A(r'.\.venv\Scripts\python.exe packaging/apply_adjudication.py')
    A('```')
    A('')
    A('会把 `口径判定` / `确认单位` 两列回填到候选记录，并重算哪些记录可以进入训练。')
    A('')

    (ap / 'adjudication_ledger.md').write_text('\n'.join(L), encoding='utf-8')

    print(f'台账 → adjudication_ledger.csv（{len(rows)} 组 / {len(usable)} 条候选）')
    print(f'可读版 → adjudication_ledger.md')
    print()
    print('  自动预判分布:')
    for k, v in Counter(r['自动预判'] for r in rows).most_common():
        n = sum(r['条数'] for r in rows if r['自动预判'] == k)
        print(f'    {k:12} {v:>2} 组 / {n:>3} 条')
    print()
    print('  需人工逐组确认的（预判=待定）:')
    for r in rows:
        if r['自动预判'] == '待定':
            print(f"    {r['条数']:>3} 条 · 「{r['表格标题(caption)'][:50]}」")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
