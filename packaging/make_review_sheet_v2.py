"""把 v2 候选整理成人工可逐条核对的审阅表（含难负例预览）。

标签审计必须由人做。本表把每条候选的全部证据摆在一起：
原句 + 页码 + 表内本期/上期/变动 + 三值校验 + 匹配强度 + 难负例清单，
让判定者不需要回翻 PDF 就能完成大部分判断。
"""
from __future__ import annotations

import csv
import json
import sys
import argparse
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent

# 逐条要回答的问题（对应要求文档的审计维度）
QUESTIONS = [
    '原句是否确实在陈述可核验结论（不是标题/表头/残句/行业数据）？',
    'metric 与正文用词是否同义？（营收/销售收入/营业收入 不得自动判等）',
    'value 与该表单元格是否一致？',
    'unit 是否为表格显式声明？（元 vs 万元差 10⁴）',
    'period（本期/上期）与表头列是否对应？',
    'scope 口径：这条事实是合并数、母公司数还是分部数？正文引用的是同一口径吗？',
    '若为增长率类：是否确实需要「本期+上期」两个来源？增长率句是否指向本指标？',
    '难负例是否真的与正例不同源（错指标/错主体/错口径）？',
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path,
                        default=ROOT / '答辩评测' / 'annual_reports_new_20261002')
    parser.add_argument('--out', type=Path,
                        help='输出目录；默认写回 corpus 目录')
    args = parser.parse_args()
    corpus = args.corpus
    out = args.out or corpus
    out.mkdir(parents=True, exist_ok=True)
    cs = [json.loads(l) for l in (corpus / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    usable = [c for c in cs if c['claim_kind'] in ('growth', 'quote')]
    abstain = [c for c in cs if c['claim_kind'] == 'abstain']
    usable.sort(key=lambda c: (c['company'], c['fact_page'] or 0))

    rows = []
    for i, c in enumerate(usable, 1):
        rows.append({
            '序号': i,
            '类型': '增长率' if c['claim_kind'] == 'growth' else '本期引用',
            '公司代码': c['company'],
            '公司名': c['company_name'],
            '指标(表格原文)': c['metric'],
            '本期值': c['value'],
            '上期值': c['fact_prior_value'],
            '报告变动': c['fact_change_value'],
            '单位': c['unit'],
            '口径': c['scope'],
            '正文页': c['claim_page'],
            '表页': c['fact_page'],
            '列判定': c['column_method'],
            '三值校验': c['three_value_check'],
            '匹配强度': c['match_level'],
            '难负例数': c['negative_pool_size'],
            '同义词候选': '/'.join(c.get('synonym_candidates') or []),
            '原句': c['claim_text'],
            '人工判定': '',
            '口径确认(合并/母公司/分部)': '',
            '单位确认(元/万元)': '',
            '判定人': '',
            '判定时间': '',
            '备注': '',
        })

    with (out / 'review_sheet_v2.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    L = []
    A = L.append
    A('# v2 候选人工核对表')
    A('')
    A(f'**可用候选 {len(usable)} 条**（增长率 {sum(1 for c in usable if c["claim_kind"]=="growth")}、'
      f'本期引用 {sum(1 for c in usable if c["claim_kind"]=="quote")}）')
    A(f'涉及公司 **{len({c["company"] for c in usable})}** 家 · 另附拒答样本 {len(abstain)} 条')
    A('')
    A('> `label` 均为 `null`。本表是**待审**清单，不是训练集。')
    A('> 统计数字是自动抽取条数，不是已确认样本数。')
    A('')
    A('## 每条要回答的 8 个问题')
    A('')
    for i, q in enumerate(QUESTIONS, 1):
        A(f'{i}. {q}')
    A('')
    A('## 关键结构说明')
    A('')
    A('- **增长率类**：中文年报正文的典型句式是「实现营业收入 X 元，较上年同期增长 Y%」——')
    A('  给本期绝对值与增长率，**不给上期绝对值**（上期在表格第二列）。')
    A('  因此这类样本的**正例是一个来源集合 {本期事实, 上期事实}**，')
    A('  评价用「集合完整率 + 集合精确匹配」，不是 Top-1。')
    A('  上期来源主要由此产生，本期:上期 = '
      f'{len(usable)}:{sum(1 for c in usable if c["claim_kind"]=="growth")}。')
    A('- **本期引用类**：正文只出现本期绝对值，正例是单个本期事实。')
    A('- **口径（第 6 问）是本次审计的重点**：自动判定几乎无法从表格本身得出合并/母公司，')
    A('  必须由人工根据章节位置与表标题确认。')
    A('')
    A('## 逐条')
    A('')

    cur = None
    for r in rows:
        if r['公司代码'] != cur:
            cur = r['公司代码']
            A(f"### {cur} {r['公司名']}")
            A('')
        A(f"**[{r['序号']}] {r['类型']} · {r['指标(表格原文)']}**")
        A('')
        A(f"- 原句（正文第 {r['正文页']} 页）：「{r['原句']}」")
        A(f"- 表格（第 {r['表页']} 页）：本期 {r['本期值']} ／ 上期 {r['上期值']} ／ 报告变动 {r['报告变动']}")
        A(f"- 单位 `{r['单位']}` · 口径 `{r['口径']}` · 匹配强度 `{r['匹配强度']}` · 三值 `{r['三值校验']}`")
        A(f"- 难负例 {r['难负例数']} 条可用"
          + (f" · 同义词候选 {r['同义词候选']}（**不得自动判等**）" if r['同义词候选'] else ''))
        A('')
        A('| 判定 | 说明 |')
        A('|---|---|')
        A('| ☐ 接受 | 8 问全部确认 |')
        A('| ☐ 仅口径待定 | 其余通过 |')
        A('| ☐ 仅单位待定 | 其余通过 |')
        A('| ☐ 拒绝 | 原因：____________ |')
        A('')

    A('## 拒答样本（单列，不计入正例）')
    A('')
    A(f'共 {len(abstain)} 条：正文出现指标名但**数值对不上任何表格事实**。')
    A('用途：训练模型「无可验证来源时弃答」，避免被迫选择（任务书第 9 项）。')
    A('已过滤掉"表格行标签被当成句子"的污染（原 862 条，过滤后 345 条）。')
    A('')
    A('样例：')
    for c in abstain[:6]:
        A(f"- {c['company']} {c['metric']}：「{(c['claim_text'] or '')[:80]}」")
    A('')
    A('## 自动统计（供对照，非结论）')
    A('')
    A(f"- 三值自洽 {sum(1 for c in usable if c['three_value_check']=='consistent')}/{len(usable)}")
    A(f"- 匹配 exact {sum(1 for c in usable if c['match_level']=='exact')}/{len(usable)}")
    A(f"- 口径已标明 {sum(1 for c in usable if c['scope']!='未标明')}/{len(usable)} ← 审计重点")
    A(f"- 难负例池合计 {sum(c['negative_pool_size'] for c in usable)} 条")
    A(f"- 涉及公司 {len({c['company'] for c in usable})} 家")
    A('')

    (out / 'review_sheet_v2.md').write_text('\n'.join(L), encoding='utf-8')

    print(f'审阅表 → review_sheet_v2.md（{len(usable)} 条）')
    print(f'CSV    → review_sheet_v2.csv')
    print(f'拒答   → {len(abstain)} 条（单列）')
    print(f'涉及公司 {len({c["company"] for c in usable})} 家')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
