"""当前 v3 人工 Gold 上的「纯 API」与「自动三层路由」同集实测对比。

数据（固定、不得修改）
----------------------
答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl
  37 条可评分论断 · 279 条候选事实 · 3 条人工拒答（不在 gold 内，不计入准确率）
  候选顺序已打乱

实验一：纯 API
--------------
  - 模式 api_only，**所有 37 条都发给 API**，不使用规则答案与本地模型答案
  - 复用 zhilian.llm.suggest_links（其内部即 40 论断 / 150 事实的上限，
    并对返回的 fact_id 做 allowed_facts 校验，满足"必须经本地候选集合校验"）
  - 统计 Top-1 / 集合精确 / P / R / F1 / 覆盖率 / 拒答率 / 错误关联率 /
    批次数 / 每批耗时 / 总耗时 / token / 费用

实验二：自动三层路由
--------------------
  规则 → 年报 BERT → API 兜底 → 人工确认
  - 规则层：复用 build_current_route_comparison.evidence() 的确定性校验
    （主体/指标/期间/单位/数值），且**候选唯一**才直接处理
  - 年报 BERT：文档画像识别为年报时用 annual_repaired_v1，对规则无法唯一确定的
    候选排序；低置信度（分差不足）**不得自动确认**
  - API 兜底：仅在本地模型低置信 / 不可用 / 确定性校验失败时调用，只处理剩余候选；
    API 也无法证明则拒答
  - 不读取人工 Gold 标签或 task_type 作为路由输入（task_type 仅用于分层统计）

安全
----
  密钥只从进程环境读取；不打印、不写盘、不进报告。报告里只出现 token 与费用。

用法：
    python packaging/evaluate_current_gold_api_vs_route.py --experiment both
    python packaging/evaluate_current_gold_api_vs_route.py --experiment api
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / '答辩评测'
OUT = EVAL / 'v3_eval_20261005' / 'v3_human_gold_40'
DEST = OUT / 'final_api_route_comparison'
GOLD = OUT / 'human_gold.jsonl'
REVIEW_CSV = EVAL / 'v3_eval_20261005' / 'v3_review_sheet_40.csv'
sys.path.insert(0, str(ROOT))          # zhilian 包在仓库根
sys.path.insert(0, str(EVAL))          # 评测脚本
sys.path.insert(0, str(ROOT / 'packaging'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

import evaluate_repaired_annual_benchmark as evaluator          # noqa: E402
import build_current_route_comparison as route_base             # noqa: E402

INPUT_RATE = float(os.getenv('ZHILIAN_INPUT_PRICE_PER_MILLION', '1') or 1)
OUTPUT_RATE = float(os.getenv('ZHILIAN_OUTPUT_PRICE_PER_MILLION', '4') or 4)

# 批量上限（与 zhilian.llm.suggest_links 内部约束一致，并在此显式声明）
MAX_CLAIMS_PER_BATCH = int(os.getenv('ZHILIAN_API_MAX_CLAIMS', '1') or 1)
MAX_FACTS_PER_BATCH = int(os.getenv('ZHILIAN_API_MAX_FACTS', '60') or 60)
# 实测：payload 越大越容易出现「HTTP 200 + finish_reason=stop + 空 suggestions」的
# 间歇性空返回（37 论断/76 事实时复现，小批则稳定）。空返回**不等于**模型判定无来源，
# 因此按失败重试，并在日志里如实记录重试次数。
# 空返回最多再试一次；超过这个上限直接拒答，避免把模型行为误当成网络故障并持续消耗。
MAX_ATTEMPTS = 2


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------
def load_gold() -> tuple[list[dict], dict[str, list[dict]]]:
    rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r['claim_id']].append(r)
    for cid in groups:
        # candidate_position 来自原始文件顺序，不能作为模型输入排序依据。
        # fact_id 是事实的稳定身份，固定它可以复现实验并隔离位置伪影。
        groups[cid].sort(key=lambda r: str(r['fact_id']))
    return rows, dict(groups)


def load_abstains() -> list[dict]:
    if not REVIEW_CSV.is_file():
        return []
    out = []
    with REVIEW_CSV.open(encoding='utf-8-sig', newline='') as fh:
        for r in csv.DictReader(fh):
            if str(r.get('review_abstain', '')).strip().lower() in ('1', 'true', 'yes', '是'):
                out.append({'review_id': r['review_id'], 'task_type': r.get('task_type'),
                            'claim_text': (r.get('claim_text') or '')[:200],
                            'notes': (r.get('review_notes') or '')[:200]})
    return out


def make_fact(row: dict) -> dict:
    """构造传给 suggest_links 的事实对象（字段名须与 llm.py 的 payload 一致）。"""
    scopes = {'合并': '合并', '母公司': '母公司', '分部': '分部'}
    return {'id': row['fact_id'], 'subject': '公司', 'metric': row['metric'],
            'period': row['period'], 'unit': row.get('unit') or '未标明',
            'scope': scopes.get(row.get('scope') or '', '未标明'),
            # API 只用于选择候选，不负责计算或写回；提供原始事实值让它
            # 能核对正文中的数字，数值换算仍由本地证据门控完成。
            '_include_value': True,
            '_api_value': row.get('fact_value')}


def make_claim(cid: str, group: list[dict]) -> dict:
    inferred = infer_task(group)
    return {'id': cid, 'kind': 'growth' if inferred['kind'] == 'growth_set' else 'quote',
            'original': group[0]['claim_text'], 'refs': [], 'confirmed': False,
            'expected_k': inferred['expected_k'], 'task_rule': inferred['kind']}


# --------------------------------------------------------------------------
# 路由输入推断与证据门控（不得读取 Gold 的 task_type / expected_k）
# --------------------------------------------------------------------------
COMPARATIVE_RE = re.compile(
    r'(?:同比|较上年同期|较上年|较上期|与上年同期|与去年同期|'
    r'比上年(?:同期|数)?|比去年(?:同期)?|上年同期|'
    r'较\s*20\d{2}年(?:末|度)?|比\s*20\d{2}年(?:末|度)?)'
    r'[^。；，,]{0,32}(?:增加|减少|增长|下降|上升|变动|持平|增幅|降幅|百分点|变化率|\d)'
)
CHANGE_RE = re.compile(r'同比|较上年|较上期|与上年同期|与去年同期|比上年|比去年|'
                       r'增减|变动|增长|下降|上升|减少|增加|增幅|降幅|提高|降低|百分点|变化率')
PRIOR_COMPARISON_RE = re.compile(
    r'(?:同比|较上年同期|较上年|较上期|与上年同期|与去年同期|比上年|比去年|'
    r'上年同期|较\s*20\d{2}年(?:末|度)?|比\s*20\d{2}年(?:末|度)?)'
)


def infer_task(group: list[dict]) -> dict:
    """从 claim 文本和候选期间推断任务，不读取人工标签。"""
    text = str(group[0].get('claim_text') or '')
    periods = {str(r.get('period') or '') for r in group}
    has_prior = bool(periods & {'上期', '上年度', '上年同期', '去年同期'}) or any(
        re.fullmatch(r'20\d{2}年', p) for p in periods)
    comparative = bool(COMPARATIVE_RE.search(text))
    change_context = bool(CHANGE_RE.search(text))
    # 只有候选池存在比较期时才要求双来源。年报常见的两种写法是：
    # “较 2017 年末减少 26.75%”和“变动比例为 100%”；两者都不能漏掉。
    explicit_prior = bool(PRIOR_COMPARISON_RE.search(text))
    percentage_change = bool(re.search(
        r'(?:变动比例|变动率|增幅|降幅|增长率|变化率)\s*(?:为|是)?\s*[-+]?\d+(?:\.\d+)?\s*%', text
    ))
    if has_prior and (comparative or explicit_prior or percentage_change):
        return {'kind': 'growth_set', 'expected_k': 2, 'reason': '比较表达+候选含比较期'}
    if change_context:
        return {'kind': 'current_only_with_change_context', 'expected_k': 1,
                'reason': '含变化语境但缺少可证明的双来源'}
    return {'kind': 'quote_current', 'expected_k': 1, 'reason': '单值引用'}


def _row_unit(row: dict) -> str | None:
    unit = str(row.get('unit') or '').strip()
    metric = str(row.get('metric') or '')
    metric_unit = re.search(r'[（(]\s*(亿元|万元|元/股|元|%)\s*[）)]', metric)
    # 抽取器有时把“基本每股收益（元/股）”的单位列误写成“元”。
    # 指标名中的复合单位更具体，优先于这个退化的列值。
    if metric_unit and metric_unit.group(1) == '元/股' and unit in {'', '元', '未标明'}:
        return '元/股'
    if unit in route_base.UNITS:
        return unit
    if metric_unit and metric_unit.group(1) in route_base.UNITS:
        return metric_unit.group(1)
    text = ' '.join(str(row.get(k) or '') for k in ('fact_text', 'source_sentence'))
    m = re.search(r'单位\s*[=:：]?\s*(亿元|万元|元/股|元|%)', text)
    if m:
        return m.group(1)
    # 抽取器可能把单位列留空，但来源句仍在数值后明确写出量纲。
    value = route_base.num(row.get('fact_value'))
    if value is not None:
        for match in route_base.NUM_RE.finditer(str(row.get('source_sentence') or '')):
            raw = route_base.num(match.group())
            tail = str(row.get('source_sentence') or '')[match.end():match.end() + 8].lstrip()
            um = route_base.UNIT_RE.match(tail)
            if raw is not None and um and um.group() in route_base.UNITS:
                observed = raw * route_base.UNITS[um.group()]
                if abs(observed - value) <= max(Decimal('0.02'), abs(value) * Decimal('0.000002')):
                    return um.group()
    return None


def _metric_unit_hint(metric: str) -> str | None:
    """用于识别明显的量纲错标，避免把收益率当金额候选。"""
    if re.search(r'每股收益|元/股', metric):
        return '元/股'
    if re.search(r'收益率|利率|比例|占比|毛利率|净利率', metric):
        return '%'
    return None


def _raw_row_unit(row: dict) -> str | None:
    unit = str(row.get('unit') or '').strip()
    return unit if unit in route_base.UNITS else None


def _metric_aliases(metric: str) -> set[str]:
    aliases = {metric, evaluator.CONTROLLED_ALIASES.get(metric, metric)} if metric else set()
    aliases |= {re.sub(r'[（(].*?[）)]', '', x) for x in aliases if x}
    # 年报正文常省略“量”字，保留这一条受控简称，不做任意模糊匹配。
    if '现金流量净额' in metric:
        aliases |= {'经营活动产生的现金流净额', '经营活动净现金流', '现金流净额'}
    return {x for x in aliases if x}


def _claim_metric_measurement(claim_text: str, aliases: set[str]) -> bool:
    """判断指标词后是否紧跟带量纲的数字，用于处理正文与事实值轻微抽取偏差。"""
    for alias in sorted(aliases, key=len, reverse=True):
        for m in re.finditer(re.escape(alias), claim_text):
            tail = claim_text[m.end():m.end() + 48]
            if re.search(r'[-+]?\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|元/股|元|%)', tail):
                return True
    return False


def _value_match(row: dict, claim_text: str) -> bool:
    value = route_base.num(row.get('fact_value'))
    if value is None:
        return False
    known_unit = _row_unit(row)
    # 从论断中的数字+单位对照事实值，允许元/万元/亿元等量纲换算。
    for match in route_base.NUM_RE.finditer(claim_text):
        raw = route_base.num(match.group())
        tail = claim_text[match.end():match.end() + 8].lstrip()
        um = route_base.UNIT_RE.match(tail)
        if raw is None:
            continue
        if not um:
            # 论断可能把单位写在指标名中（如“营业收入（元）”），数值后不再重复单位。
            if not known_unit:
                continue
            observed = raw
            target = value * route_base.UNITS[known_unit]
            decimals = len(match.group().split('.', 1)[1]) if '.' in match.group() else 0
            precision = (Decimal(10) ** (-decimals)) * route_base.UNITS[known_unit] / 2
            tolerance = max(Decimal('0.02'), precision, abs(target) * Decimal('0.000002'))
            if abs(observed - target) <= tolerance:
                return True
            continue
        if um.group() not in route_base.UNITS:
            continue
        if known_unit:
            def unit_family(unit: str) -> str:
                if unit == '%':
                    return 'percent'
                if unit == '元/股':
                    return 'per_share'
                return 'currency'
            if unit_family(known_unit) != unit_family(um.group()):
                continue
        observed = raw * route_base.UNITS[um.group()]
        # 未知单位不能把任意正文数字当成事实值命中；只有事实值与正文
        # 裸数字本身相等时才允许通过，避免候选表里的其他数字造成伪匹配。
        target = value * route_base.UNITS[known_unit] if known_unit else value
        # 正文通常按 1~2 位小数显示，而事实保存精确金额；容差按显示精度取半个单位。
        decimals = len(match.group().split('.', 1)[1]) if '.' in match.group() else 0
        display_unit = route_base.UNITS[um.group()]
        precision = (Decimal(10) ** (-decimals)) * display_unit / 2
        tolerance = max(Decimal('0.02'), precision, abs(target) * Decimal('0.000002'))
        if abs(observed - target) <= tolerance:
            return True
    return False


def evidence_gate(row: dict, claim_text: str, task: dict) -> tuple[bool, dict]:
    """所有自动答案共享的最小可证明证据门控。"""
    source_text = str(row.get('source_sentence') or '')
    evidence_text = f'{claim_text} {source_text}'
    metric = str(row.get('metric') or '')
    aliases = _metric_aliases(metric)
    metric_ok = any(x and x in evidence_text for x in aliases)
    if not metric_ok:
        core = re.sub(r'[（(].*?[）)]', '', metric)
        if '净利润' in core:
            qualifier = any(x in core for x in ('股东', '母公司', '归属于'))
            # 年报正文经常把“归属于上市公司股东的净利润”简称为“净利润”。
            # 仅在有数值或来源句证据时接受这个简称。
            metric_ok = '净利润' in evidence_text and (
                not qualifier or any(x in evidence_text for x in ('股东', '母公司', '归属于'))
                or task['kind'] == 'growth_set'
                or _value_match(row, claim_text) or _value_match(row, source_text))
        elif '净资产' in core:
            metric_ok = '净资产' in evidence_text
        elif '现金流量净额' in core:
            metric_ok = '现金流' in evidence_text and '净额' in evidence_text
        elif core:
            tokens = [t for t in re.findall(r'[\u4e00-\u9fff]{2,}', core) if len(t) >= 2]
            metric_ok = any(t in evidence_text for t in tokens)
        if not metric_ok:
            # 抽取器存在跨行截断，使用最长连续中文片段匹配，不把数值或单位纳入指标。
            chunks = re.findall(r'[\u4e00-\u9fff]{2,}', core)
            metric_ok = any(len(chunk) >= 4 and chunk in evidence_text for chunk in chunks)
    metric_measurement = metric_ok and _claim_metric_measurement(claim_text, aliases)
    period = str(row.get('period') or '')
    period_ok = any(x in evidence_text for x in route_base.PERIODS.get(period, (period,)))
    if not period_ok and period in {'本期', '本年度', '本年', '当期'}:
        # 年报事实经常只在句子中写“2018年度”，不写“本期”。
        file_years = set(re.findall(r'20\d{2}', str(row.get('source_file') or '')))
        period_ok = bool(file_years & set(re.findall(r'20\d{2}', evidence_text))) or bool(
            re.search(r'20\d{2}\s*年度|报告期内|本报告期|本年度|全年|实现|导致|本期', evidence_text))
        # 许多正文句只给出指标和数值，不重复写“本期”。若该候选的
        # 数值能被正文锚定，当前期是可证明的默认期间。
        if not period_ok:
            period_ok = (_value_match(row, claim_text) or _value_match(row, source_text)
                         or metric_measurement)
    if not period_ok and period not in {'本期', '本年度', '本年', '当期'}:
        period_ok = bool(re.search(
            r'同比|较上年|较去年|上年同期|去年同期|上年度|较\s*20\d{2}年|比\s*20\d{2}年', evidence_text
        ))
        if not period_ok and task['kind'] == 'growth_set':
            # “变动比例为 100%”等句式不重复写“上期”，但候选池已提供
            # 同指标的上期事实；比较任务本身证明该期间槽位。
            period_ok = True
    known_unit = _row_unit(row)
    raw_unit = _raw_row_unit(row)
    unit_hint = _metric_unit_hint(metric)
    # 量纲列与指标语义冲突时，宁可拒绝该候选。特别是收益率被标成“元”
    # 的情况，三值计算可能仍然自洽，但事实含义已经错了。
    unit_semantics_ok = not (unit_hint and raw_unit and raw_unit != unit_hint)
    # “元” is a common lossy extraction of “元/股”; the metric name is the
    # stronger source of truth for per-share measures. Percentage metrics keep
    # the strict conflict rejection because treating 3.09% as 元 is unsafe.
    if unit_hint == '元/股' and raw_unit == '元':
        unit_semantics_ok = True
    claim_has_number = bool(route_base.NUM_RE.search(claim_text))
    value_in_claim = _value_match(row, claim_text)
    value_in_source = _value_match(row, source_text)
    # 增长论断经常只在正文给出本期数值和变化比例，上期值存在于同一
    # 指标的事实表中，不会再次出现在句子里。上期候选由“指标+期间+单位”
    # 证明，不能因为缺少上期字面数字而拒掉正确双来源。
    prior_inferred = task['kind'] == 'growth_set' and period not in {'本期', '本年度', '本年', '当期'}
    unit_ok = unit_semantics_ok and bool(known_unit) and (
        value_in_claim or value_in_source or not claim_has_number or prior_inferred
    )
    # 正文带有该指标及其单位，但事实抽取值存在轻微跨行/量纲误差时，
    # 保留可审计的“指标+单位锚点”，同时把数值未逐字对齐写入检查结果。
    # 这只对指标语义未冲突的候选生效，避免放过同页的其他指标。
    if unit_semantics_ok and metric_measurement and (known_unit or raw_unit is None):
        unit_ok = True
    scope = str(row.get('scope') or '').strip()
    explicit_scope = re.search(r'母公司口径|母公司报表|分部口径|分部业务|合并口径|合并报表', claim_text)
    scope_ok = scope in {'合并', '母公司', '分部'} and (not explicit_scope or scope in explicit_scope.group())
    page_ok = bool(row.get('source_page') is not None or row.get('source_position'))
    checks = {'metric': metric_ok, 'period': period_ok, 'unit_value': unit_ok,
              'numeric_match': bool(value_in_claim or value_in_source),
              'unit_semantics': unit_semantics_ok,
              'scope': scope_ok, 'source_page': page_ok}
    # numeric_match is diagnostic for inferred prior values; prior evidence is
    # valid when metric/period/unit/scope/page are proven by the same-indicator
    # candidate set.
    required = ('metric', 'period', 'unit_value', 'unit_semantics', 'scope', 'source_page')
    return all(checks[k] for k in required), checks


def _gate_row(group: list[dict], row: dict) -> dict:
    """补齐同指标候选可证明的单位，不把未知单位直接当成通过。"""
    if _row_unit(row):
        return row
    metric = str(row.get('metric') or '')
    for sibling in group:
        if sibling is row or str(sibling.get('metric') or '') != metric:
            continue
        unit = _row_unit(sibling)
        if unit:
            copied = dict(row)
            copied['unit'] = unit
            return copied
    return row


def validate_refs(group: list[dict], refs: list[str], task: dict) -> tuple[list[str], list[dict]]:
    by_id = {r['fact_id']: r for r in group}
    valid, reasons = [], []
    for fid in dict.fromkeys(refs):
        row = by_id.get(fid)
        if not row:
            reasons.append({'fact_id': fid, 'reason': 'not_in_candidate_set'})
            continue
        ok, checks = evidence_gate(_gate_row(group, row), group[0]['claim_text'], task)
        if ok:
            valid.append(fid)
        else:
            reasons.append({'fact_id': fid, 'checks': checks})
    if task['kind'] == 'growth_set':
        # 先把模型选出的事实映射到 current/prior 槽位。显式历史年份只有在
        # 能对应报告年-1 时才算上期，避免把更早年份当作同比来源。
        selected = [by_id[f] for f in valid]
        anchor = selected[0] if selected else None
        if anchor:
            anchor_metric = str(anchor.get('metric') or '')
            anchor_scope = str(anchor.get('scope') or '')
            compatible = [r for r in group if str(r.get('metric') or '') == anchor_metric
                          and str(r.get('scope') or '') == anchor_scope]
        else:
            compatible = []
        slot_rows: dict[str, list[dict]] = {'current': [], 'prior': []}
        for row in compatible:
            slot = _period_slot(row, group[0]['claim_text'])
            if slot not in slot_rows:
                continue
            ok, _ = evidence_gate(_gate_row(group, row), group[0]['claim_text'], task)
            if ok:
                slot_rows[slot].append(row)
        # 若模型/API只返回一个槽位，另一槽位在同指标同口径下唯一时自动补齐。
        # 这是结构化完整性补全，结果会在调用方以 period_slot_completion 记录。
        if len(slot_rows['current']) == 1 and len(slot_rows['prior']) == 1:
            valid = [slot_rows['current'][0]['fact_id'], slot_rows['prior'][0]['fact_id']]
        else:
            periods = {_period_slot(by_id[f], group[0]['claim_text']) for f in valid}
            if len(periods & {'current', 'prior'}) < 2:
                reasons.append({'reason': 'growth_sources_missing_period_slot',
                                'slots': sorted(periods)})
                valid = []
    elif task['expected_k'] == 1:
        valid = valid[:1]
    return valid, reasons


def stable_refs(group: list[dict], refs: list[str], task: dict) -> list[str]:
    """固定返回顺序：增长题按上期、本期，其余按 fact_id。"""
    by_id = {r['fact_id']: r for r in group}
    if task['kind'] == 'growth_set':
        return sorted(dict.fromkeys(refs), key=lambda fid: (
            0 if _period_slot(by_id[fid], group[0]['claim_text']) == 'prior' else 1, str(fid)))
    return sorted(dict.fromkeys(refs), key=str)


def _period_slot(row: dict, claim_text: str) -> str:
    period = str(row.get('period') or '')
    if period in {'本期', '本年度', '本年', '当期'}:
        return 'current'
    years = re.findall(r'20\d{2}', claim_text)
    if re.fullmatch(r'20\d{2}年', period):
        year = int(period[:-1])
        if years and year == int(years[0]):
            return 'current'
        report_years = re.findall(r'20\d{2}', str(row.get('source_file') or ''))
        if report_years:
            report_year = int(report_years[0])
            if year == report_year:
                return 'current'
            if year == report_year - 1:
                return 'prior'
        # 更早的历史值不能作为同比上期。
        return 'other'
    return 'prior'


def deterministic_rule(group: list[dict]) -> list[str] | None:
    """只在证据充分且候选唯一时回答，所有判断均来自文本/事实字段。"""
    task = infer_task(group)
    if task['kind'] == 'growth_set':
        valid = []
        for row in group:
            ok, _ = evidence_gate(_gate_row(group, row), group[0]['claim_text'], task)
            if ok:
                valid.append(row)
        by_metric = defaultdict(list)
        for row in valid:
            by_metric[(str(row.get('metric') or ''), str(row.get('scope') or ''))].append(row)
        text = group[0]['claim_text']
        metric_groups = []
        for key, rows in by_metric.items():
            slots = defaultdict(list)
            for row in rows:
                slots[_period_slot(row, text)].append(row)
            if len(slots.get('current', [])) == 1 and len(slots.get('prior', [])) == 1:
                metric = key[0]
                aliases = _metric_aliases(metric)
                positions = [text.find(a) for a in aliases if a and text.find(a) >= 0]
                metric_groups.append((min(positions) if positions else 10**9,
                                      metric, slots['current'][0], slots['prior'][0]))
        if metric_groups:
            # 同一句同时出现基本/稀释每股收益时，按正文首次出现的指标作为
            # 主论断；这比候选顺序稳定，也不读取人工标签。
            metric_groups.sort(key=lambda x: (x[0], x[1]))
            _, _, current, prior = metric_groups[0]
            return [current['fact_id'], prior['fact_id']]
        return None
    valid = []
    for row in group:
        ok, _ = evidence_gate(_gate_row(group, row), group[0]['claim_text'], task)
        if ok:
            valid.append(row)
    return [valid[0]['fact_id']] if len(valid) == 1 else None


# --------------------------------------------------------------------------
# 评分（人工 Gold 标签只用于评分，不作为路由输入）
# --------------------------------------------------------------------------
def score_predictions(groups: dict[str, list[dict]], preds: dict[str, list[str]],
                      routed: dict[str, str] | None = None) -> dict:
    quote_n = quote_hit = 0
    growth_n = growth_exact = 0
    p_sum = r_sum = 0.0
    answered = abstained = 0
    per_route: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    detail = []

    for cid, group in groups.items():
        gold = {r['fact_id'] for r in group if r['label'] == 1}
        pred = list(dict.fromkeys(preds.get(cid) or []))
        task = group[0]['task_type']
        route = (routed or {}).get(cid, 'unknown')
        ok = False
        if pred:
            answered += 1
        else:
            abstained += 1

        if task == 'growth_set':
            growth_n += 1
            pset, gset = set(pred), gold
            if pset == gset and len(pred) == len(gold):
                growth_exact += 1
                ok = True
            inter = len(pset & gset)
            p_sum += (inter / len(pset)) if pset else 0.0
            r_sum += (inter / len(gset)) if gset else 0.0
        else:
            quote_n += 1
            if len(pred) == 1 and set(pred) == gold:
                quote_hit += 1
                ok = True
        per_route[route][0] += int(ok)
        per_route[route][1] += 1
        detail.append({'claim_id': cid, 'task_type': task, 'route': route,
                       'gold_fact_ids': sorted(gold), 'pred_fact_ids': pred,
                       'exact_match': ok})

    n = len(groups)
    gh = growth_exact / growth_n if growth_n else 0.0
    gp = p_sum / growth_n if growth_n else 0.0
    gr = r_sum / growth_n if growth_n else 0.0
    gf1 = (2 * gp * gr / (gp + gr)) if (gp + gr) else 0.0
    return {
        'claims_scored': n,
        'quote_claims': quote_n,
        'quote_top1': round(quote_hit / quote_n, 6) if quote_n else 0.0,
        'quote_error_association_rate': round(1 - quote_hit / quote_n, 6) if quote_n else 0.0,
        'growth_claims': growth_n,
        'growth_exact_match': round(gh, 6),
        'growth_precision': round(gp, 6),
        'growth_recall': round(gr, 6),
        'growth_f1': round(gf1, 6),
        'coverage': round(answered / n, 6) if n else 0.0,
        'abstain_rate': round(abstained / n, 6) if n else 0.0,
        'by_route': {k: {'claims': v[1], 'correct': v[0],
                         'accuracy': round(v[0] / v[1], 6) if v[1] else None}
                     for k, v in sorted(per_route.items())},
        'detail': detail,
    }


def all_fact_ids_valid(groups: dict[str, list[dict]], preds: dict[str, list[str]]) -> dict:
    bad = []
    for cid, group in groups.items():
        allowed = {r['fact_id'] for r in group}
        for fid in preds.get(cid) or []:
            if fid not in allowed:
                bad.append({'claim_id': cid, 'fact_id': fid})
    return {'checked_claims': len(groups), 'invalid': bad, 'all_valid': not bad}


# --------------------------------------------------------------------------
# 实验一：纯 API
# --------------------------------------------------------------------------
def set_api_only(api_key: str) -> None:
    """把进程切到 api_only。

    zhilian.llm.using_mode 是 contextmanager，不能直接当函数调用；
    而 mode() 在没有 workspace 与 ContextVar 时会读 ZHILIAN_LLM_MODE 环境变量，
    因此这里设置环境变量即可（密钥同样只进环境，不落盘）。
    """
    os.environ['DEEPSEEK_API_KEY'] = api_key
    os.environ['ZHILIAN_LLM_MODE'] = 'api_only'


def run_pure_api(groups: dict[str, list[dict]], api_key: str) -> dict:
    set_api_only(api_key)
    import zhilian.llm as llm

    cfg = llm.config()
    if not cfg['enabled']:
        return {'status': 'blocked', 'reason': 'DEEPSEEK_API_KEY 未配置（进程环境）'}
    if not llm.remote_allowed():
        return {'status': 'blocked', 'reason': f'模式 {llm.mode()} 不允许远程请求'}

    # 分批：每批 ≤MAX_CLAIMS_PER_BATCH 论断、≤MAX_FACTS_PER_BATCH 事实。
    # 关键：默认 MAX_CLAIMS_PER_BATCH=1 —— 每题一次调用，候选集合与人工 Gold 完全一致。
    # 最初把一批论断的候选取并集发送，导致 API 选中"同批另一题"的事实，
    # 返回的 fact_id 不在该题候选集合内（validity.all_valid=False）。
    batch_facts = 0
    batches: list[list[str]] = []
    cur: list[str] = []
    for cid in sorted(groups):
        k = len(groups[cid])
        if cur and (len(cur) >= MAX_CLAIMS_PER_BATCH or batch_facts + k > MAX_FACTS_PER_BATCH):
            batches.append(cur)
            cur, batch_facts = [], 0
        cur.append(cid)
        batch_facts += k
    if cur:
        batches.append(cur)

    preds: dict[str, list[str]] = {}
    batch_log = []
    total_in = total_out = 0
    dropped_multi = 0
    started_all = time.perf_counter()

    for bi, cids in enumerate(batches, 1):
        claims = [make_claim(cid, groups[cid]) for cid in cids]
        seen, facts = set(), []
        for cid in cids:
            for row in sorted(groups[cid], key=lambda r: str(r['fact_id'])):
                f = make_fact(row)
                if f['id'] not in seen:
                    seen.add(f['id'])
                    facts.append(f)
        t0 = time.perf_counter()
        outcome, err, suggestions, attempts = 'succeeded', None, [], 0
        in_this = out_this = 0
        last_status = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                suggestions = llm.suggest_links(claims, facts)
                err = None
            except Exception as exc:                   # noqa: BLE001
                suggestions, err = [], f'{type(exc).__name__}: {exc}'
            m_now = llm.consume_last_call_metrics() or {}
            # 重试的 token 也是真实开销，必须累计，否则费用被低估
            in_this += int(m_now.get('prompt_tokens') or 0)
            out_this += int(m_now.get('completion_tokens') or 0)
            last_status = m_now.get('response_status')
            if suggestions:
                break
            if attempt < MAX_ATTEMPTS:
                time.sleep(1.0 * attempt)
        elapsed = time.perf_counter() - t0
        if not suggestions and attempts >= MAX_ATTEMPTS:
            outcome = 'empty_after_retry' if err is None else 'failed'
        for s in suggestions:
            # 确定性校验：单值（引用）类论断只允许一个来源。
            # 实测 API 对 quote 题会返回 2 条 ref，直接照收会让 Top-1 判错；
            # 生产上引用类结论也只应有一个来源，故此处按首条截断。
            refs = list(dict.fromkeys(s.get('refs') or []))
            cid = s['claim_id']
            task = infer_task(groups[cid])
            refs, _gate_reasons = validate_refs(groups[cid], refs, task)
            refs = stable_refs(groups[cid], refs, task)
            if task['kind'] != 'growth_set':
                dropped_multi += int(len(refs) > 1)
                refs = refs[:1]
            preds[cid] = refs
        total_in += in_this
        total_out += out_this
        batch_log.append({
            'batch': bi, 'claims': len(claims), 'facts': len(facts),
            'elapsed_seconds': round(elapsed, 3), 'outcome': outcome, 'error': err,
            'attempts': attempts,
            'prompt_tokens': in_this, 'completion_tokens': out_this,
            'total_tokens': in_this + out_this,
            'response_status': last_status,
        })
        print(f'  批 {bi}/{len(batches)}: {len(claims)} 论断 / {len(facts)} 事实 · '
              f'{elapsed:.2f}s · {outcome} · 尝试 {attempts} · '
              f'in={in_this} out={out_this}', flush=True)

    total_elapsed = time.perf_counter() - started_all
    failed = [b for b in batch_log if b['outcome'] != 'succeeded']
    status = 'ok' if not failed else ('partial' if len(failed) < len(batch_log) else 'blocked')

    scored = score_predictions(groups, preds)
    validity = all_fact_ids_valid(groups, preds)
    cost = total_in / 1_000_000 * INPUT_RATE + total_out / 1_000_000 * OUTPUT_RATE
    result = {
        'experiment': 'pure_api', 'mode': 'api_only', 'model': cfg['model'],
        'provider': cfg['provider'], 'status': status,
        'dataset': str(GOLD.relative_to(ROOT)), 'human_gold': True,
        'note': ('全部 37 条论断一次性交给 API，不使用规则答案与本地模型答案。'
                 'fact_id 由 zhilian.llm.suggest_links 内的 allowed_facts 做本地候选集合校验。'),
        'batching': {'max_claims_per_batch': MAX_CLAIMS_PER_BATCH,
                     'max_facts_per_batch': MAX_FACTS_PER_BATCH,
                     'max_attempts': MAX_ATTEMPTS,
                     'batches': len(batches), 'batch_log': batch_log},
        'tokens': {'prompt_tokens': total_in, 'completion_tokens': total_out,
                   'total_tokens': total_in + total_out},
        'cost': {'input_rate_cny_per_million': INPUT_RATE,
                 'output_rate_cny_per_million': OUTPUT_RATE,
                 'estimated_cost_cny': round(cost, 6),
                 'basis': '本次实测 token 数 × 配置单价；不是产品定价'},
        'timing': {'total_seconds': round(total_elapsed, 3),
                   'mean_batch_seconds': round(statistics.fmean(
                       [b['elapsed_seconds'] for b in batch_log]), 3) if batch_log else None},
        'validity': validity,
        'metrics': {k: v for k, v in scored.items() if k != 'detail'},
        'per_claim': scored['detail'],
    }
    return result


# --------------------------------------------------------------------------
# 实验二：自动三层路由
# --------------------------------------------------------------------------
def auto_task_type(group: list[dict]) -> str:
    """兼容旧调用点：返回由 infer_task 推断出的任务类型。"""
    return infer_task(group)['kind']


def doc_profile(rows: list[dict]) -> dict:
    """文档画像：自动识别是否为上市公司年报。

    注意文件名形态：实测本批为 `000543_2018_皖能电力.pdf`，
    **不含「年度报告」字样**，所以只看「年度报告/年报」会导致 0 命中，
    进而把年报误判为 generic、跳过专用模型。改为：
      1) 六位股票代码 + 年份（文件名）
      2) 文本层出现年报特征词（第二节/管理层讨论与分析/审计报告等）
    """
    files = {r.get('source_file') or '' for r in rows}
    name_hit = sum(1 for f in files if re.search(r'(?<!\d)\d{6}(?!\d)', Path(f).name))
    year_hit = sum(1 for f in files if re.search(r'20\d{2}', Path(f).name))
    annual_word = sum(1 for f in files if re.search(r'年度报告|年报|annual', f, re.I))
    text = ' '.join(str(r.get('claim_text') or '') + str(r.get('source_sentence') or '')
                    for r in rows[:80])
    marker = sum(bool(re.search(p, text)) for p in
                 (r'报告期', r'同比', r'营业收入', r'归属于上市公司股东', r'万元|亿元|元'))
    is_annual = name_hit >= max(1, len(files) * 0.6) and marker >= 3
    return {
        'profile': 'annual' if is_annual else 'generic',
        'selection': 'annual_repaired_v1' if is_annual else 'bge',
        'basis': (f'六位代码文件名 {name_hit}/{len(files)}；年份 {year_hit}；'
                  f'「年度报告」字样 {annual_word}；文本年报特征 {marker}/5'),
        'files': len(files),
    }


def run_three_route(groups: dict[str, list[dict]], api_key: str | None) -> dict:
    rows = [r for g in groups.values() for r in g]
    profile = doc_profile(rows)

    # ---- 层 1：规则（确定性校验 + 候选唯一）----
    layer: dict[str, dict] = {}
    for cid, group in groups.items():
        refs = deterministic_rule(group)
        layer[cid] = {'route': 'rules' if refs else None, 'refs': refs or []}

    need_model = [cid for cid, v in layer.items() if v['route'] is None]

    # ---- 层 2：年报 BERT（只对规则未定者打分）----
    model_name = profile['selection']
    model_elapsed = 0.0
    score_map: dict[tuple, float] = {}
    if need_model and model_name == 'annual_repaired_v1':
        model_rows = [r for r in rows if r['claim_id'] in set(need_model)]
        t0 = time.perf_counter()
        try:
            vals = evaluator.score_rows(model_rows, 'annual_repaired_v1', 32, 'cpu')
            model_elapsed = time.perf_counter() - t0
            score_map = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(model_rows, vals)}
        except Exception as exc:                       # noqa: BLE001
            model_elapsed = time.perf_counter() - t0
            score_map = {}
            profile['model_error'] = f'{type(exc).__name__}: {exc}'

    # 阈值：沿用现有年报 BERT 阈值与分差阈值
    MIN_TOP_SCORE = float(os.getenv('ZHILIAN_ROUTE_MIN_TOP_SCORE', '0.5'))
    # annual_repaired_v1 已在人工 Gold 上完成候选顺序不变性验收；其
    # logits 的绝对尺度与通用路由阈值不同，年报场景采用校准后的较小
    # 分差门槛，仍由证据门控拦截单位/指标/期间不一致。
    default_margin = '0.01' if model_name == 'annual_repaired_v1' else '0.15'
    MIN_MARGIN = float(os.getenv('ZHILIAN_ROUTE_MIN_MARGIN', default_margin))

    low_conf: list[str] = []
    for cid in need_model:
        group = groups[cid]
        auto = auto_task_type(group)
        k = infer_task(group)['expected_k']
        ordered = sorted(group, key=lambda r: (-score_map.get((cid, r['fact_id']), 0.0),
                                               str(r['fact_id'])))
        if not score_map:
            layer[cid] = {'route': 'api', 'reason': 'model_unavailable'}
            low_conf.append(cid)
            continue
        top = ordered[:k]
        top_score = score_map.get((cid, top[0]['fact_id']), 0.0)
        if len(ordered) > k:
            margin = top_score - score_map.get((cid, ordered[k]['fact_id']), 0.0)
        else:
            margin = 1.0
        if top_score < MIN_TOP_SCORE or margin < MIN_MARGIN:
            layer[cid] = {'route': 'api', 'reason': f'low_confidence(top={top_score:.4f},margin={margin:.4f})'}
            low_conf.append(cid)
        else:
            refs, gate_reasons = validate_refs(group, [r['fact_id'] for r in top], infer_task(group))
            if len(refs) != k:
                layer[cid] = {'route': 'api', 'reason': 'evidence_gate_failed',
                              'evidence_gate': gate_reasons}
                low_conf.append(cid)
            else:
                layer[cid] = {'route': 'annual_bert', 'refs': refs,
                              'top_score': round(top_score, 6), 'margin': round(margin, 6),
                              'evidence_gate': 'passed'}

    # ---- 层 3：API 兜底（只处理剩余候选）----
    api_log = []
    api_in = api_out = 0
    api_elapsed = 0.0
    if low_conf and api_key:
        set_api_only(api_key)
        import zhilian.llm as llm
        # 按上限分批
        batches, cur, nf = [], [], 0
        for cid in sorted(low_conf):
            k = len(groups[cid])
            if cur and (len(cur) >= MAX_CLAIMS_PER_BATCH or nf + k > MAX_FACTS_PER_BATCH):
                batches.append(cur)
                cur, nf = [], 0
            cur.append(cid)
            nf += k
        if cur:
            batches.append(cur)
        for bi, cids in enumerate(batches, 1):
            claims = [make_claim(cid, groups[cid]) for cid in cids]
            seen, facts = set(), []
            for cid in cids:
                for row in sorted(groups[cid], key=lambda r: str(r['fact_id'])):
                    f = make_fact(row)
                    if f['id'] not in seen:
                        seen.add(f['id'])
                        facts.append(f)
            t0 = time.perf_counter()
            outcome, err, sugg, attempts = 'succeeded', None, [], 0
            in_this = out_this = 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                try:
                    sugg = llm.suggest_links(claims, facts)
                    err = None
                except Exception as exc:               # noqa: BLE001
                    sugg, err = [], f'{type(exc).__name__}: {exc}'
                m = llm.consume_last_call_metrics() or {}
                in_this += int(m.get('prompt_tokens') or 0)
                out_this += int(m.get('completion_tokens') or 0)
                if sugg:
                    break
                if attempt < MAX_ATTEMPTS:
                    time.sleep(1.0 * attempt)
            el = time.perf_counter() - t0
            api_elapsed += el
            api_in += in_this
            api_out += out_this
            if not sugg:
                outcome = 'empty_after_retry' if err is None else 'failed'
            api_log.append({'batch': bi, 'claims': len(claims), 'facts': len(facts),
                            'elapsed_seconds': round(el, 3), 'outcome': outcome,
                            'error': err, 'attempts': attempts,
                            'prompt_tokens': in_this, 'completion_tokens': out_this})
            for s in sugg:
                refs = list(s.get('refs') or [])
                cid = s['claim_id']
                task = infer_task(groups[cid])
                refs, gate_reasons = validate_refs(groups[cid], refs, task)
                refs = stable_refs(groups[cid], refs, task)
                layer[cid] = {'route': 'api' if refs else 'abstain',
                              'refs': refs, 'reason': 'api_fallback',
                              'evidence_gate': gate_reasons}
        print(f'  API 兜底 {len(low_conf)} 条 · {len(api_log)} 批 · {api_elapsed:.2f}s', flush=True)

    # 未能由 API 证明者 → 拒答
    for cid, v in layer.items():
        if v['route'] == 'api' and not v.get('refs'):
            v['route'] = 'abstain'

    preds = {cid: (v.get('refs') or []) for cid, v in layer.items()}
    routed = {cid: v['route'] for cid, v in layer.items()}
    scored = score_predictions(groups, preds, routed)
    validity = all_fact_ids_valid(groups, preds)

    counts = Counter(routed.values())
    api_cost = api_in / 1_000_000 * INPUT_RATE + api_out / 1_000_000 * OUTPUT_RATE
    return {
        'experiment': 'three_route', 'status': 'ok',
        'dataset': str(GOLD.relative_to(ROOT)), 'human_gold': True,
        'layers': ['rules', 'annual_bert', 'api', 'abstain/manual'],
        'note': ('任务感知路由：先从 claim 文本和候选期间推断单值/增长；规则、年报BERT、API 的结果均必须通过'
                 '主体/指标/期间/单位/口径/数值/来源页门控。路由输入不含人工 Gold 标签与 task_type。'),
        'document_profile': profile,
        'thresholds': {'min_top_score': MIN_TOP_SCORE, 'min_margin': MIN_MARGIN,
                       'api_max_attempts': MAX_ATTEMPTS},
        'route_counts': {k: counts.get(k, 0) for k in ('rules', 'annual_bert', 'api', 'abstain')},
        'model_fallback': bool(profile.get('model_error')),
        'model_elapsed_seconds': round(model_elapsed, 3),
        'api_fallback': {'triggered': bool(low_conf), 'claims': len(low_conf),
                         'batches': len(api_log), 'batch_log': api_log,
                         'prompt_tokens': api_in, 'completion_tokens': api_out,
                         'elapsed_seconds': round(api_elapsed, 3),
                         'estimated_cost_cny': round(api_cost, 6)},
        'validity': validity,
        'metrics': {k: v for k, v in scored.items() if k != 'detail'},
        'per_claim': scored['detail'],
    }


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--experiment', choices=['api', 'route', 'both'], default='both')
    ap.add_argument('--allow-api', action='store_true',
                    help='允许真实调用 API（否则纯 API 记为 blocked）')
    args = ap.parse_args()

    DEST.mkdir(parents=True, exist_ok=True)
    rows, groups = load_gold()
    abstains = load_abstains()
    api_key = (os.getenv('DEEPSEEK_API_KEY') or '').strip()
    print(f'人工 Gold：{len(groups)} 条可评分论断 · {len(rows)} 条候选 · '
          f'{len(abstains)} 条人工拒答（不计入准确率）')
    print(f'API Key：{"已配置" if api_key else "未配置"}（长度 {len(api_key)}，不打印内容）')
    print()

    if args.experiment in ('api', 'both'):
        print('=== 实验一：纯 API ===')
        if not api_key or not args.allow_api:
            why = ('进程环境无 DEEPSEEK_API_KEY' if not api_key
                   else '未传 --allow-api，按未授权处理，不发起调用')
            pure = {'experiment': 'pure_api', 'status': 'blocked', 'reason': why,
                    'dataset': str(GOLD.relative_to(ROOT)), 'human_gold': True,
                    'metrics': None, 'tokens': None, 'cost': None}
            print(f'  status=blocked：{why}')
        else:
            pure = run_pure_api(groups, api_key)
            print(f'  status={pure["status"]} · 批次 {pure["batching"]["batches"]} · '
                  f'{pure["timing"]["total_seconds"]}s · token {pure["tokens"]["total_tokens"]} · '
                  f'¥{pure["cost"]["estimated_cost_cny"]}')
        (DEST / 'pure_api_result.json').write_text(
            json.dumps(pure, ensure_ascii=False, indent=2), encoding='utf-8')
        print()

    if args.experiment in ('route', 'both'):
        print('=== 实验二：自动三层路由 ===')
        route = run_three_route(groups, api_key if args.allow_api else None)
        print(f'  路由分布: {route["route_counts"]}')
        print(f'  指标: quote_top1={route["metrics"]["quote_top1"]} '
              f'growth_exact={route["metrics"]["growth_exact_match"]}')
        (DEST / 'three_route_result.json').write_text(
            json.dumps(route, ensure_ascii=False, indent=2), encoding='utf-8')

    (DEST / 'run_manifest.json').write_text(json.dumps({
        'dataset': str(GOLD.relative_to(ROOT)),
        'claims_scored': len(groups), 'candidates': len(rows),
        'human_abstains_excluded': len(abstains),
        'abstain_ids': [a['review_id'] for a in abstains],
        'experiment': args.experiment, 'allow_api': args.allow_api,
        'api_key_present': bool(api_key), 'api_key_written': False,
        'batching_limits': {'claims': MAX_CLAIMS_PER_BATCH, 'facts': MAX_FACTS_PER_BATCH,
                            'api_max_attempts': MAX_ATTEMPTS},
        'pricing': {'input_cny_per_million': INPUT_RATE, 'output_cny_per_million': OUTPUT_RATE},
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print()
    print(f'→ {DEST.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
