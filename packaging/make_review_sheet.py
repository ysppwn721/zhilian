"""把待审候选整理成人工可逐条核对的审阅表。

标签审计必须人做，但可以做的是：把每条候选的**全部证据**摆在一个人面前，
并按"需要判断什么"组织，而不是给一堆 JSON 让人自己去 PDF 里翻。

产出：
  review_sheet.md    逐条列出：原句 + 页码 + 表内本期/上期值 + 三值校验 + 待判问题
  review_sheet.csv   同内容，便于在 Excel/WPS 里勾选与统计
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / '答辩评测' / 'annual_reports_new_20261002'

# 人工需要逐条回答的问题（与要求文档的审计维度对齐）
QUESTIONS = [
    'claim_text 是否确实是一句可核验的论断（而不是标题/表头/残句）？',
    'claim_page 与 fact_page 是否指向同一主体与同一口径（合并/母公司/分部）？',
    'metric 的用词与正文用词是否同义？（营收/销售收入/营业收入 不得自动判等）',
    'value 与表格单元格是否一致？',
    'unit 是否为表格显式声明？若不是，如何确定是元还是万元？',
    'period（本期/上期）是否与表头列对应？',
    '若为增长率类，是否确实需要「本期+上期」两个来源？',
]


def main() -> int:
    cands = [json.loads(l) for l in (CORPUS / 'audit_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    review = [c for c in cands if c['audit_status'] == 'review']
    # 按公司、再按是否增长率分组，便于集中核对同一家
    review.sort(key=lambda c: (c['company'], c['fact_page'] or 0, c['metric'] or ''))

    rows = []
    for i, c in enumerate(review, 1):
        rows.append({
            '序号': i,
            '公司代码': c['company'],
            '公司名': c['company_name'],
            '文件': c['source_file'],
            '指标(表格原文)': c['metric'],
            '期间': c['period'],
            '值': c['value'],
            '单位(待核)': c['unit'],
            '口径': c['scope'],
            '正文页': c['claim_page'],
            '表页': c['fact_page'],
            '表内上期值': c['fact_prior_value'],
            '表内变动值': c['fact_change_value'],
            '列判定': c['column_method'],
            '三值校验': c['three_value_check'],
            '三值算得': c['three_value_calc'],
            '锚点类型': c['anchor_kind'],
            '待判事项': '；'.join(c.get('audit_reasons', [])),
            '原句': c['claim_text'],
            '人工判定(label)': '',
            '判定人': '',
            '判定时间': '',
            '备注': '',
        })

    csv_path = CORPUS / 'review_sheet.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- Markdown 审阅表 ----
    L = []
    A = L.append
    A('# 待审候选人工核对表')
    A('')
    A(f'共 **{len(review)}** 条。`label` 均为 `null`，需人工填写后才能进入训练集。')
    A('')
    A('## 怎么用这张表')
    A('')
    A('每条候选要回答下面 7 个问题；`review_sheet.csv` 里有对应的是/否列可直接填。')
    A('')
    for i, q in enumerate(QUESTIONS, 1):
        A(f'{i}. {q}')
    A('')
    A('> 判定规则（与任务书一致）：')
    A('> - **接受**：7 项全部确认。')
    A('> - **修单位**：仅单位未确认，其余通过 —— 核实后可用。')
    A('> - **拒绝**：任一项不成立。')
    A('> - 同义词（营收/销售收入/营业收入）在未确认前一律 **不得判等**。')
    A('')
    A('## 逐条')
    A('')

    cur_company = None
    for r in rows:
        if r['公司代码'] != cur_company:
            cur_company = r['公司代码']
            A(f"### {cur_company} {r['公司名']}")
            A('')
            A(f"文件：`{r['文件']}`")
            A('')
        A(f"**[{r['序号']}] {r['指标(表格原文)']}** · {r['期间']} · "
          f"{r['值']} {r['单位(待核)'] or '（单位未知）'} · 口径：{r['口径']}")
        A('')
        A(f"- 原句（正文第 {r['正文页']} 页）：「{r['原句']}」")
        A(f"- 表格（第 {r['表页']} 页）：本期 {r['值']} / 上期 {r['表内上期值']} / 变动 {r['表内变动值']}")
        A(f"- 列判定：`{r['列判定']}` · 三值校验：`{r['三值校验']}`"
          + (f"（算得 {r['三值算得']}）" if r['三值算得'] is not None else ''))
        A(f"- 锚点类型：`{r['锚点类型']}`")
        A(f"- **待判**：{'；'.join(r['待判事项'])}")
        A('')
        A('| 判定 | 说明 |')
        A('|---|---|')
        A('| ☐ 接受 | 7 项全部确认 |')
        A('| ☐ 修单位后可用 | 仅单位待核 |')
        A('| ☐ 拒绝 | 附原因：____________ |')
        A('')

    A('## 汇总统计（自动，供对照）')
    A('')
    from collections import Counter
    A(f"- 待判事项分布：`{dict(Counter(x for r in rows for x in r['待判事项'].split('；') if x))}`")
    A(f"- 本期锚点 {sum(1 for r in rows if r['锚点类型']=='current_anchor')} · "
      f"上期锚点 {sum(1 for r in rows if r['锚点类型']=='prior_anchor')}")
    A(f"- 涉及公司 {len({r['公司代码'] for r in rows})} 家")
    A('')
    A('> 提醒：本表是**待审**清单，不是训练集。上表统计是自动抽取条数，不是已确认样本数。')
    A('')

    (CORPUS / 'review_sheet.md').write_text('\n'.join(L), encoding='utf-8')

    print(f'审阅表 → {CORPUS / "review_sheet.md"}（{len(review)} 条）')
    print(f'CSV    → {csv_path}')
    print(f'涉及公司 {len({r["公司代码"] for r in rows})} 家')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
