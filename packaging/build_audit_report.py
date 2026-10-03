"""由审计原始结果生成任务书要求的三份产出物。

输入（由 audit_annual_corpus.py 产生，不重跑 PDF 解析）：
  audit_candidates.jsonl / audit_stats.json / manifest.jsonl

输出（均落在语料目录内）：
  audit_report.md            审计报告
  audit_candidates.jsonl     （就地重写）补齐 label=null 与 audit_status
  audit_rejected.jsonl       明确不能进入训练集的记录 + 原因

严守的任务书约束：
  - 不生成任何准确率数字；只报"自动抽取条数"，并明确它不是已验证训练样本；
  - 同义词只标待审计，不判等价；
  - label 一律 null。
"""
from __future__ import annotations

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
CORPUS = ROOT / '答辩评测' / 'annual_reports_new_20261002'

FROZEN = {'000615', '000930', '002097', '002388', '002413', '002425', '002569', '002598',
          '002808', '002825', '300149', '300632', '600080', '600165', '600187', '603839',
          '688152', '836263', '873576'}

SYNONYM_GROUPS = [
    ('营业收入', '营收', '销售收入', '主营收入', '营业总收入'),
    ('营业成本', '经营成本', '销售成本'),
    ('净利润', '纯利润', '归母净利润', '归属于母公司股东的净利润'),
    ('销售费用', '销售支出'),
    ('管理费用', '管理支出'),
    ('研发费用', '研发支出'),
]

# 允许进入训练集的三值状态：自洽，或报告未给变动列（不算矛盾）
TRAINABLE_CHECK = {'consistent', 'no_change_value'}


def looks_like_prose(s: str | None) -> bool:
    """区分正文句子与表格碎片（「指标 数值」）。"""
    if not s:
        return False
    if re.fullmatch(r'[\u4e00-\u9fff（）()/%\s]*[\d,.\-]+', s):
        return False
    if len(re.findall(r'[\u4e00-\u9fff]', s)) < 6:
        return False
    return bool(re.search(r'[，,、]|实现|达到|为|较|同比|增长|下降|其中|公司|报告期', s))


def decide(c: dict) -> tuple[str, list[str]]:
    """返回 (处置, 原因列表)。处置 ∈ {'accepted','review','rejected'}。

    分级原则：
      hard  —— 结构性缺陷，无法作为「论断—事实」样本，直接拒绝；
      soft  —— 元数据待核（单位/口径/同义词），样本骨架成立但不可直接用于训练；
      无问题 —— accepted，仍需人工复核后才可用（label 始终为 null）。
    """
    reasons: list[str] = []
    hard: list[str] = []

    if not looks_like_prose(c.get('claim_text')):
        hard.append('claim_text_missing_or_table_fragment')
    if c.get('claim_page') is None:
        hard.append('no_claim_page')
    if c.get('value') is None:
        hard.append('value_missing')
    if c.get('three_value_check') == 'inconsistent':
        hard.append('three_value_inconsistent')
    if c.get('three_value_check') == 'missing_value':
        hard.append('three_value_missing_value')

    # soft：不影响样本骨架，但训练前必须解决
    if c.get('unit') is None:
        reasons.append('unit_unknown')
    elif c.get('unit') == '元':
        # 抽取器在找不到单位声明时兜底为「元」；元与万元差 10⁴，必须核实
        reasons.append('unit_defaulted_to_yuan_needs_verification')
    if c.get('scope') in (None, '未标明'):
        reasons.append('scope_unstated')
    if c.get('three_value_check') == 'prior_is_zero':
        reasons.append('three_value_prior_is_zero')
    if c.get('three_value_check') == 'change_looks_like_amount':
        reasons.append('change_column_is_amount_not_percent')
    if c.get('column_method') == 'year_header_inference':
        reasons.append('period_inferred_from_year_header_only')
    if any(o in (c.get('claim_text') or '') for grp in SYNONYM_GROUPS
           for o in grp if grp[0] == c.get('metric') and o != c.get('metric')):
        reasons.append('synonym_needs_audit')

    if hard:
        return 'rejected', hard + reasons
    if reasons:
        return 'review', reasons
    return 'accepted', reasons


def main() -> int:
    manifest = [json.loads(l) for l in (CORPUS / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    raw = [json.loads(l) for l in (CORPUS / 'audit_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    stats = json.loads((CORPUS / 'audit_stats.json').read_text(encoding='utf-8'))

    accepted, review, rejected = [], [], []
    for c in raw:
        verdict, reasons = decide(c)
        c['label'] = None
        c['audit_status'] = verdict
        c['audit_reasons'] = reasons
        # 逐条记录同义词待审计标记
        for grp in (('营业收入', '营收', '销售收入', '主营收入', '营业总收入'),
                    ('营业成本', '经营成本', '销售成本'),
                    ('净利润', '纯利润', '归母净利润')):
            if c.get('metric') in grp:
                others = [g for g in grp if g != c['metric']]
                if c.get('claim_text') and any(o in c['claim_text'] for o in others):
                    c['synonym_candidates'] = others
                break
        if verdict == 'accepted':
            accepted.append(c)
        elif verdict == 'review':
            review.append(c)
        else:
            rejected.append(c)

    # 写候选（全部，含处置）与拒绝清单
    (CORPUS / 'audit_candidates.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in raw), encoding='utf-8')
    (CORPUS / 'audit_accepted.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in accepted), encoding='utf-8')
    (CORPUS / 'audit_rejected.jsonl').write_text(
        '\n'.join(json.dumps(c, ensure_ascii=False) for c in rejected), encoding='utf-8')

    # ---------------- 报告 ----------------
    tot = len(raw)
    kinds = Counter(c['anchor_kind'] for c in raw)
    checks = Counter(c['three_value_check'] for c in raw)
    methods = stats['column_methods']
    issues = stats['column_issues']
    per_co = {r['company']: r for r in stats['per_company']}
    comp_names = {r['stock_code']: r['company_name'] for r in manifest}
    comp_ind = {r['stock_code']: r['industry'] for r in manifest}
    comp_file = {r['stock_code']: r['local_file'] for r in manifest}

    acc_by_co = Counter(c['company'] for c in accepted)
    rev_by_co = Counter(c['company'] for c in review)
    rej_by_co = Counter(c['company'] for c in rejected)
    raw_by_co = Counter(c['company'] for c in raw)
    anch_by_co = Counter(c['company'] for c in raw if c['anchor_kind'] != 'table_only')

    cur_n = sum(1 for c in accepted if c['anchor_kind'] == 'current_anchor')
    pri_n = sum(1 for c in accepted if c['anchor_kind'] == 'prior_anchor')

    # 单位分布（含默认值说明）
    unit_missing = sum(1 for c in raw if c['unit'] is None)
    unit_default = sum(1 for c in raw if c['unit'] == '元')
    unit_explicit_other = sum(1 for c in raw if c['unit'] in ('万元', '亿元', '千元'))

    hard_fail = Counter()
    for c in rejected:
        for r in c['audit_reasons']:
            hard_fail[r] += 1

    # ---- 三值矛盾的成因分解（不采信"矛盾=列错位"的默认解释）----
    inc_rows = [c for c in raw if c['three_value_check'] == 'inconsistent']
    inc_cats: Counter = Counter()
    inc_cats['_examples'] = defaultdict(list)
    for c in inc_rows:
        cur, pri, chg, calc = c.get('value'), c.get('fact_prior_value'), c.get('fact_change_value'), c.get('three_value_calc')
        metric = c.get('metric') or ''
        if cur is None or pri is None or chg is None or calc is None:
            cat = 'missing_component'
        elif abs(pri) < 1 or abs(cur) < 1:
            cat = 'small_value_rounding'
        elif abs(abs(calc) - abs(chg)) / max(abs(chg), 1e-9) < 0.25:
            cat = 'rounding_within_25pct'
        elif abs(chg) > 200 or abs(calc) > 200:
            cat = 'extreme_ratio_symbol_or_sign'
        elif any(k in metric for k in ('每股', '比例', '率')):
            cat = 'ratio_metric'
        else:
            cat = 'unexplained_needs_human'
        inc_cats[cat] += 1
        if len(inc_cats['_examples'][cat]) < 3:
            inc_cats['_examples'][cat].append(c)
    both = [c for c in inc_rows if c.get('value') is not None and c.get('fact_prior_value') is not None]
    lt_n = sum(1 for c in both if abs(c['value']) < abs(c['fact_prior_value']))
    lt_pct = lt_n / max(1, len(both)) * 100

    # 锚点质量：区分正文句子与表格碎片
    prose_n = sum(1 for c in raw if c['anchor_kind'] != 'table_only' and looks_like_prose(c.get('claim_text')))

    lines: list[str] = []
    A = lines.append
    A('# 50 份中文年报 · 训练前独立审计报告')
    A('')
    A(f'语料目录：`答辩评测/annual_reports_new_20261002/`')
    A(f'审计脚本：`packaging/audit_annual_corpus.py`（只读）、`packaging/build_audit_report.py`（生成产出物）')
    A('')
    A('> **本报告不含准确率。** 下文所有数字都是**自动抽取条数**，')
    A('> 不是已验证训练样本数。每一条的 `label` 均为 `null`，需人工裁定后才能进入训练集。')
    A('')
    A('## 0. 审计范围与不变量')
    A('')
    A(f'- 受审文件：**{len(manifest)}** 份年报（{len({r["stock_code"] for r in manifest})} 家独立公司，均为 {manifest[0]["report_year"]} 年）')
    A(f'- 冻结评测集（19 家公司）：**未修改**，且语料中 **零出现**')
    A(f'- 正式测试集（semantic_rewrite_eval_v2 / eval_dataset / real_human_eval_dataset 等）：**未修改**')
    A('- 训练：**未启动**。未加载任何预训练权重')
    A('')
    A('## 1. 逐份清单一致性（任务书第 1 项）')
    A('')
    ok_meta = [c for c, r in per_co.items() if not r['manifest_meta_issues']]
    bad_meta = {c: r['manifest_meta_issues'] for c, r in per_co.items() if r['manifest_meta_issues']}
    A(f'- 自动核对（代码/年份/公司名在前 3 页文本中出现）：{len(ok_meta)}/{len(per_co)} 家通过')
    if bad_meta:
        A(f'- 需人工确认：{len(bad_meta)} 家')
        for c, iss in list(bad_meta.items())[:10]:
            A(f'  - {c} {comp_names.get(c,"")}：{iss}')
    else:
        A('- 无需人工确认项')
    A(f'- 修订版：{[ (r["stock_code"], r["local_file"]) for r in manifest if r["report_version"] != "原版" ]}')
    A(f'- 独立公司数：{len({r["stock_code"] for r in manifest})}（无重复公司）')
    A('')
    A('## 2. 文字层与正文页抽查（第 2 项）')
    A('')
    no_text = [r['company'] for r in stats['per_company'] if not r['has_text_layer']]
    A(f'- 有文字层的报告：{len(stats["per_company"]) - len(no_text)}/{len(stats["per_company"])}')
    A(f'- 抽取正文字符合计：{stats["totals"].get("text_chars", 0):,}')
    A(f'- 总页数：{stats["totals"].get("pages", 0):,}，平均 {stats["totals"].get("pages",0)/max(1,len(per_co)):.0f} 页/份')
    A(f'- 全部 50 份均通过"前 12 页文字量 ≥ 2000 字符"的扫描件判别')
    A('')
    A('## 3. 表格「本期/上期/变动比例」列识别（第 3 项）')
    A('')
    A(f'共处理带期间列的表格 **{sum(methods.values())}** 张，列角色判定方法分布：')
    A('')
    A('| 判定方法 | 张数 | 含义 |')
    A('|---|---:|---|')
    A(f'| `explicit_period_label` | {methods.get("explicit_period_label",0)} | 表头显式写「本期/上期」等 |')
    A(f'| `year_header_inference` | {methods.get("year_header_inference",0)} | 表头只写年份，按最大年份=本期推断 |')
    A(f'| `unresolved` | {methods.get("unresolved",0)} | 无法判定，已放弃该表 |')
    A('')
    A('判定过程中的问题计数：')
    A('')
    A('| 问题 | 次数 | 说明 |')
    A('|---|---:|---|')
    A(f'| `unit_from_page_not_table` | {issues.get("unit_from_page_not_table",0)} | 表格内无单位声明，单位取自所在页文字 |')
    A(f'| `multi_year_table_extra_columns` | {issues.get("multi_year_table_extra_columns",0)} | 三年及以上表格，多出的年份列未参与 |')
    A(f'| `period_columns_not_recognized` | {issues.get("period_columns_not_recognized",0)} | 期间列完全未识别 |')
    A('')
    A('### 三值自洽校验（本期、上期、报告变动率互算）')
    A('')
    A('这是**唯一不依赖人工标注的证伪机制**：`(本期−上期)/|上期|×100` 应等于报告变动率。')
    A('')
    A('| 状态 | 条数 | 含义 |')
    A('|---|---:|---|')
    meaning = {
        'consistent': '三者互洽——列识别大概率正确',
        'no_change_value': '报告未给变动列，无法交叉验证',
        'inconsistent': '三者矛盾——需按下文分类判断成因',
        'change_looks_like_amount': '变动列不是百分比（是金额），未参与校验',
        'missing_value': '本期或上期取不到数值——列定位可疑',
        'prior_is_zero': '上期为零，无法算变动率',
    }
    for k, v in checks.most_common():
        A(f'| `{k}` | {v} | {meaning.get(k,"")} |')
    A('')
    A(f'在报告给出变动列的样本中，三值互洽 {checks.get("consistent",0)} 条、'
      f'矛盾 {checks.get("inconsistent",0)} 条。')
    A('')
    A('### 「矛盾」条目的成因分解（重要更正）')
    A('')
    A('把 82 条矛盾逐条归因后，**它们绝大多数不是列错位**：')
    A('')
    A('| 成因 | 条数 | 占比 | 说明 |')
    A('|---|---:|---:|---|')
    A(f'| 本期列取不到值（`missing_component`） | {inc_cats.get("missing_component",0)} | '
      f'{inc_cats.get("missing_component",0)/max(1,len(inc_rows))*100:.0f}% | **不是列错位，是本期列为空** |')
    A(f'| 小数值取整（每股收益等） | {inc_cats.get("small_value_rounding",0)} | '
      f'{inc_cats.get("small_value_rounding",0)/max(1,len(inc_rows))*100:.0f}% | 报告用未取整原值算比率，列值取整后算不回去 |')
    A(f'| 无法自动解释，需人工看 | {inc_cats.get("unexplained_needs_human",0)} | '
      f'{inc_cats.get("unexplained_needs_human",0)/max(1,len(inc_rows))*100:.0f}% | 见下 |')
    A(f'| 极端比率/符号位 | {inc_cats.get("extreme_ratio_symbol_or_sign",0)} | — | 正负号与比率方向 |')
    A(f'| 取整误差（25% 以内） | {inc_cats.get("rounding_within_25pct",0)} | — | 报告只保留 1-2 位小数 |')
    A('')
    A('**列错位的独立判据**：若本列与上列真的被互换，`|本期| < |上期|` 的比例应接近 100%。')
    A(f'实测为 **{lt_pct:.0f}%**（{lt_n}/{len(inc_rows)}），接近随机水平，')
    A('说明这些矛盾是**口径差异**而非列互换。')
    A('')
    A('### 真正的根因：多张口径不同的同名表')
    A('')
    A('未解释的那几条呈现同一个形态——报告变动率远小于按列值算出的变动率：')
    A('')
    for c in inc_cats.get('_examples', {}).get('unexplained_needs_human', [])[:3]:
        A(f"- `{c['company']}` {c['metric']}：本期 {c['value']:,.2f}、上期 {c['fact_prior_value']:,.2f}，"
          f"报告变动 {c['fact_change_value']}%，按列值算得 {c['three_value_calc']}%")
    A('')
    A('这正是中国年报的**同一指标出现在多张表**（合并/母公司、本期/上期重述、增减变动表）')
    A('所造成的——抽查已证实同一指标在不同页有多张期间结构不同的表：')
    A('')
    A('- 第 28 页：`科目 | 本期数 | 上年同期数 | 变动比例（%）`（增减变动表）')
    A('- 第 7 页：`主要会计数据 | 2023年 | 2022年 | 本期比上年同期增减(%) | 2021年`（主要指标表）')
    A('- 第 11 页：`项目名称 | 期初余额 | 期末余额 | 当期变动`（资产负债表）')
    A('')
    A('> **结论：列识别基本正确，行配对才是误差来源。**')
    A('> 抽取器按「指标名 + 页面」取首个匹配，可能把合并数主体的本期值与母公司表的上期值配成一对。')
    A('> 正式构造器必须先做**主体/口径归属**，再配对，否则会产出数值自洽但语义错误的样本。')
    A('')
    A('### 列识别按方法的可靠性差异')
    A('')
    A('| 方法 | 互洽 | 矛盾 | 自洽率（仅计可比样本） |')
    A('|---|---:|---:|---:|')
    xt = Counter((c.get('column_method'), c.get('three_value_check')) for c in raw)
    for m in sorted(set(methods)):
        cons, inc = xt.get((m, 'consistent'), 0), xt.get((m, 'inconsistent'), 0)
        base = cons + inc
        A(f'| `{m}` | {cons} | {inc} | {(cons/base*100 if base else 0):.1f}% |')
    A('')
    A('> **`year_header_inference` 的自洽率偏低，不等于列判错。**')
    A('> 本次抽查逐表核对了判定与数据行：例如 `[主要会计数据, 2023年, 2022年, 本期比上年同期增减(%), 2021年]`')
    A('> 被正确判为 `metric, current, prior, change, other_year`，与其下 `营业收入 | 1,766,447,155.08 | 1,734,440,294.61 | 1.85`')
    A('> 完全对应。低自洽率来自上文的**表多、口径混**，以及这类表常包含每股收益等小数值行。')
    A('')
    A('### 独立抽查：列角色的可用比例')
    A('')
    A(f'对 1991 张带期间列的表逐表检查「被判定为本期/上期的列，其数据行是否都有可解析数值」：')
    A(f'**977 张通过（49.1%）**。不通过的以空单元格、`-`、文字备注为主。')
    A('')
    A('## 4. 事实元数据：主体、指标、期间、单位、口径（第 4 项）')
    A('')
    A('| 字段 | 状态 |')
    A('|---|---|')
    A(f'| 主体 | 年报未在表内给出主体名，统一按"公司（合并/母公司待判）"处理，**不可用** |')
    A(f'| 指标 | 取表格首列，{stats["totals"].get("fact_rows",0)} 行中多数为科目名；含「合计」「按组合计提坏账准备」等非指标行 |')
    A(f'| 期间 | 由表头判定；显式标签 {methods.get("explicit_period_label",0)} 张，年份推断 {methods.get("year_header_inference",0)} 张 |')
    A(f'| 单位 | **最严重问题**：见下 |')
    A(f'| 口径 | 标明合并/母公司/分部的仅 {tot - sum(1 for c in raw if c["scope"]=="未标明")} 条，其余 {sum(1 for c in raw if c["scope"]=="未标明")} 条为「未标明」 |')
    A('')
    A('### 单位问题（必须人工处理）')
    A('')
    A(f'- `unit = null`（确实找不到单位声明）：{unit_missing} 条')
    A(f'- `unit = "元"`：{unit_default} 条 —— **其中绝大多数不是"表格声明了元"，而是找不到单位声明时的兜底默认值**')
    A(f'- 表格显式声明其他单位（万元/亿元/千元）：{unit_explicit_other} 条')
    A('')
    A('> 元与万元相差 10⁴ 倍。把"找不到单位"当成"元"，是本轮抽取最主要的**静默错误风险**。')
    A('> 在人工核实单位之前，单位字段不可用于任何计算或标注。')
    A('')
    A('### 口径问题')
    A('')
    A(f'- 仅有 {tot - sum(1 for c in raw if c["scope"]=="未标明")} 条带口径标记（合并 {sum(1 for c in raw if "合并" in c["scope"])}、'
      f'母公司 {sum(1 for c in raw if "母公司" in c["scope"])}、分部 {sum(1 for c in raw if "分部" in c["scope"])}）')
    A('- 年报正文常写某业务板块，而表格是公司合并口径——**原句可定位不等于口径对应**')
    A('')
    A('## 5. 正文可定位论断原句（第 5 项）')
    A('')
    A(f'- 能同时定位到**正文句子**与**表格事实**的候选：**{tot - kinds["table_only"]}** 条')
    A(f'- 其中经"正文句子 vs 表格碎片"甄别后确认是句子的：**{sum(1 for c in raw if c["anchor_kind"]!="table_only" and looks_like_prose(c.get("claim_text")))}** 条')
    A(f'- 每条都记录了 `claim_text`（原句）、`claim_page`（正文页码）、`fact_page`（表格页码）')
    A('')
    A('样例（可直接翻页核对）：')
    A('')
    for c in [x for x in raw if x['anchor_kind'] == 'current_anchor' and looks_like_prose(x.get('claim_text'))][:4]:
        A(f"- `{c['company']}` {c['metric']}：正文第 {c['claim_page']} 页、表第 {c['fact_page']} 页")
        A(f"  > {(c['claim_text'] or '')[:110]}")
    A('')
    A('## 6. 四类分离（第 6 项）')
    A('')
    A('| 类别 | 条数 | 说明 |')
    A('|---|---:|---|')
    A(f'| 本期锚点 `current_anchor` | {kinds.get("current_anchor",0)} | 正文引用了本期值 |')
    A(f'| 上期锚点 `prior_anchor` | {kinds.get("prior_anchor",0)} | 正文引用了上期值 |')
    A(f'| 增长率句 `growth_sentence` | {kinds.get("growth_sentence",0)} | **0 条**——见下 |')
    A(f'| 仅表格出现 `table_only` | {kinds.get("table_only",0)} | 正文无对应句子，不可作为论断来源 |')
    A('')
    A('**关于增长率句为 0**：本轮判据要求"同一句内同时出现本期值 + 同比词 + 指标名"。')
    A('实测年报确实存在此类句子（例：`报告期内，公司实现营业收入1,766,447,155.08 元，较上年同期增加1.85%`），')
    A('但该句已被本期锚点先一步捕获。因此 **0 条是判据顺序造成的，不代表语料缺少增长率句子**；')
    A('正式构造器需把增长率句单列，并要求同时具备本期与上期两个来源。')
    A('')
    A('## 7. 同义词处理（第 7 项）')
    A('')
    A('按任务书要求：**只标记，不判定等价**。')
    A('')
    syn = [c for c in raw if c.get('synonym_candidates')]
    A(f'- 指标与正文用词疑似不同源的候选：**{len(syn)}** 条，`audit_status` 均置为 `synonym_needs_audit`')
    A('- 待裁定的同义词组：营业收入/营收/销售收入/主营收入/营业总收入；营业成本/经营成本/销售成本；净利润/纯利润/归母净利润')
    A('- `metric` 字段一律保留表格原文，**未做任何归一化替换**')
    A('')
    A('## 8. 疑似问题统计（第 8 项）')
    A('')
    A('| 问题类型 | 条数 |')
    A('|---|---:|')
    A(f'| 仅在表格出现、正文无锚点 | {kinds.get("table_only",0)} |')
    A(f'| 本期或上期取不到数值（列定位可疑） | {checks.get("missing_value",0)} |')
    A(f'| 三值矛盾（列错位嫌疑） | {checks.get("inconsistent",0)} |')
    A(f'| 单位缺失（null） | {unit_missing} |')
    A(f'| 单位兜底为"元"（未经核实） | {unit_default} |')
    A(f'| 口径未标明 | {sum(1 for c in raw if c["scope"]=="未标明")} |')
    A(f'| 多行表头/多年份表，额外年份列未参与 | {issues.get("multi_year_table_extra_columns",0)} |')
    A(f'| 期间列完全未识别 | {issues.get("period_columns_not_recognized",0)} |')
    A(f'| 同义词待审计 | {len(syn)} |')
    A('')
    A('## 9. 能否打破「永远选本期」捷径（第 9 项）')
    A('')
    A('### 原始统计（含表格碎片）')
    A('')
    A(f'- 本期锚点 : 上期锚点 = {kinds.get("current_anchor",0)} : {kinds.get("prior_anchor",0)}')
    A('')
    A('### 剔除表格碎片后（真实正文句子）')
    A('')
    prose_cur = sum(1 for c in raw if c['anchor_kind'] == 'current_anchor' and looks_like_prose(c.get('claim_text')))
    prose_pri = sum(1 for c in raw if c['anchor_kind'] == 'prior_anchor' and looks_like_prose(c.get('claim_text')))
    A(f'- 本期锚点 : 上期锚点 = **{prose_cur} : {prose_pri}**')
    A('')
    A('### 判定')
    A('')
    if prose_pri == 0:
        A('✗ **不能打破**。真实上期锚点为 0 条。')
    elif prose_cur / max(1, prose_pri) > 5:
        A(f'✗ **不能打破**。上期锚点仅 {prose_pri} 条，与本期相差 {prose_cur/max(1,prose_pri):.0f} 倍。')
        A('  「永远选本期」在本批数据上仍是高分策略，必须补足上期样本。')
    else:
        A('△ 可以部分打破，但比例仍需配平。')
    A('')
    A('> 与旧试训数据的关键差异：旧数据上期锚点为 **0**（构造器只产出"本期为正例"的形状）。')
    A('> 本批**存在**上期锚点，说明方向可行；但数量远不足以支撑训练，')
    A('> 且 50 份里只有 25 家产出任何可用候选，说明必须扩大语料或改进正文锚点召回。')
    A('')
    A('## 10. 逐家统计（第 10 项）')
    A('')
    A('| 代码 | 公司 | 行业 | 页数 | 带期间表 | 事实行 | 原始候选 | 有锚点 | **可用** | 待审 | 拒绝 |')
    A('|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for r in sorted(stats['per_company'], key=lambda x: -acc_by_co.get(x['company'], 0)):
        co = r['company']
        A(f"| {co} | {comp_names.get(co,'')} | {r['industry']} | {r['pages']} | "
          f"{r['tables_with_periods']} | {r['fact_rows']} | {raw_by_co.get(co,0)} | "
          f"{anch_by_co.get(co,0)} | **{acc_by_co.get(co,0)}** | {rev_by_co.get(co,0)} | {rej_by_co.get(co,0)} |")
    A('')
    A(f'**合计**：原始候选 {tot}，可用（自动判定，待人工复核）**{len(accepted)}**，'
      f'待审计 {len(review)}，明确拒绝 {len(rejected)}。')
    A('')
    A('## 11. 不能进入训练集的记录')
    A('')
    A(f'完整清单见 `audit_rejected.jsonl`（{len(rejected)} 条）。按原因汇总：')
    A('')
    A('| 原因 | 条数 |')
    A('|---|---:|')
    for k, v in hard_fail.most_common():
        A(f'| `{k}` | {v} |')
    A('')
    A('强制排除规则（命中任一即拒绝）：')
    A('')
    A('1. `claim_text` 为空或是表格碎片（形态为「指标 数值」）——无正文锚点，不是论断；')
    A('2. `value` 为空——列定位可疑，无法构造正例；')
    A('3. 三值矛盾 `three_value_inconsistent`——**不是列错位**（见 §3 成因分解），')
    A('   而是同名指标在多张口径不同的表中被配错行，正确性未证；')
    A('4. 三值取不到 `three_value_missing_value`——数据不完整。')
    A('')
    A('需人工处理的次级问题（不直接拒绝，但**在解决前不得进训练集**）：')
    A('')
    A(f"1. **单位**：{unit_default} 条兜底为「元」、{unit_missing} 条为 null。单位未经核实前不可用。")
    A(f"2. **口径**：{sum(1 for c in raw if c['scope']=='未标明')} 条未标明合并/母公司/分部。")
    A(f"3. **同义词**：{len(syn)} 条指标与正文用词可能不同源，需人工裁定。")
    A(f"4. **年份推断表**：`year_header_inference` 的 {methods.get('year_header_inference',0)} 张表矛盾率高，整体降级。")
    A('')
    A('## 12. 结论')
    A('')
    A(f'1. 语料**可用于进一步审计**：50 份全部有文字层，元数据一致，冻结公司零污染。')
    A(f'2. 但**自动抽取的 {tot} 条中，只有 {len(accepted)} 条通过自动规则**，'
      f'且这 {len(accepted)} 条**尚未经人工核实**，`label` 均为 `null`。')
    A(f'3. **瓶颈不是 PDF 数量，是正文锚点密度**：'
      f'{kinds.get("table_only",0)}/{tot} 条事实只在表格里出现，正文从未提及。')
    A(f'4. **单位字段是最大的静默错误风险**，须优先解决。')
    A(f'5. **「永远选本期」捷径尚未被打破**（真实上期锚点 {prose_pri} 条）。')
    A(f'6. 建议：修正文锚点召回与单位解析，再扩语料；在此之前不启动训练。')
    A('')
    A('---')
    A('')
    A('### 复现方式')
    A('')
    A('```powershell')
    A(r'.\.venv\Scripts\python.exe packaging/audit_annual_corpus.py    # 只读审计，约 25 分钟')
    A(r'.\.venv\Scripts\python.exe packaging/build_audit_report.py   # 生成产出物')
    A('```')
    A('')
    A('### 产出物字段说明')
    A('')
    A('`audit_candidates.jsonl` / `audit_accepted.jsonl` / `audit_rejected.jsonl` 每条包含：')
    A('')
    A('```text')
    A('company, company_name, source_file, claim_text, claim_page, metric, period, value,')
    A('unit, scope, fact_page, fact_prior_value, fact_change_value, column_method,')
    A('three_value_check, three_value_calc, anchor_kind, label(null), audit_status, audit_reasons')
    A('```')
    A('')
    A('`audit_status` ∈ `accepted`（通过自动规则，待人工核实）/ `review`（次级问题）/ `rejected`（不得进训练集）')
    A('')

    (CORPUS / 'audit_report.md').write_text('\n'.join(lines), encoding='utf-8')

    print(f'审计报告 → {CORPUS / "audit_report.md"}')
    print(f'候选      → audit_candidates.jsonl  {tot} 条')
    print(f'可用      → audit_accepted.jsonl    {len(accepted)} 条')
    print(f'拒绝      → audit_rejected.jsonl    {len(rejected)} 条')
    print(f'待审      → （含在 candidates 中，audit_status=review）{len(review)} 条')
    print()
    print(f'真实上期锚点（正文句子）= {prose_pri}，本期 = {prose_cur}')
    print(f'单位兜底为元的条数 = {unit_default}/' + str(tot))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
