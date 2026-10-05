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
MAX_RETRY = int(os.getenv('ZHILIAN_API_MAX_RETRY', '3') or 3)


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------
def load_gold() -> tuple[list[dict], dict[str, list[dict]]]:
    rows = [json.loads(l) for l in GOLD.read_text(encoding='utf-8').splitlines() if l.strip()]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r['claim_id']].append(r)
    for cid in groups:
        groups[cid].sort(key=lambda r: r['candidate_position'])
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
            'scope': scopes.get(row.get('scope') or '', '未标明')}


def make_claim(cid: str, group: list[dict]) -> dict:
    return {'id': cid, 'kind': 'growth' if group[0]['task_type'] == 'growth_set' else 'quote',
            'original': group[0]['claim_text'], 'refs': [], 'confirmed': False}


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
            for row in groups[cid]:
                f = make_fact(row)
                if f['id'] not in seen:
                    seen.add(f['id'])
                    facts.append(f)
        t0 = time.perf_counter()
        outcome, err, suggestions, attempts = 'succeeded', None, [], 0
        in_this = out_this = 0
        last_status = None
        for attempt in range(1, MAX_RETRY + 1):
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
            if attempt < MAX_RETRY:
                time.sleep(1.0 * attempt)
        elapsed = time.perf_counter() - t0
        if not suggestions and attempts >= MAX_RETRY:
            outcome = 'empty_after_retry' if err is None else 'failed'
        for s in suggestions:
            # 确定性校验：单值（引用）类论断只允许一个来源。
            # 实测 API 对 quote 题会返回 2 条 ref，直接照收会让 Top-1 判错；
            # 生产上引用类结论也只应有一个来源，故此处按首条截断。
            refs = list(dict.fromkeys(s.get('refs') or []))
            cid = s['claim_id']
            if auto_task_type(groups[cid]) != 'growth_set':
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
    """自动判定任务类型（不读 task_type 标签）。"""
    text = group[0]['claim_text']
    growth = re.search(r'同比|较上年|较上期|与上年同期|增减|变动|增长|下降|上升|减少|增加|'
                       r'增幅|降幅|提高|降低|百分点|变化率', text)
    return 'growth_set' if growth else 'quote_current'


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
        refs = route_base.unique_rule(group)
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
    MIN_MARGIN = float(os.getenv('ZHILIAN_ROUTE_MIN_MARGIN', '0.15'))

    low_conf: list[str] = []
    for cid in need_model:
        group = groups[cid]
        auto = auto_task_type(group)
        k = int(group[0].get('expected_k') or (2 if auto == 'growth_set' else 1))
        ordered = sorted(group, key=lambda r: (-score_map.get((cid, r['fact_id']), 0.0),
                                               r['candidate_position']))
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
            layer[cid] = {'route': 'annual_bert', 'refs': [r['fact_id'] for r in top],
                          'top_score': round(top_score, 6), 'margin': round(margin, 6)}

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
                for row in groups[cid]:
                    f = make_fact(row)
                    if f['id'] not in seen:
                        seen.add(f['id'])
                        facts.append(f)
            t0 = time.perf_counter()
            outcome, err, sugg, attempts = 'succeeded', None, [], 0
            in_this = out_this = 0
            for attempt in range(1, MAX_RETRY + 1):
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
                if attempt < MAX_RETRY:
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
                layer[s['claim_id']] = {'route': 'api' if refs else 'abstain',
                                        'refs': refs, 'reason': 'api_fallback'}
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
        'note': ('规则→年报BERT→API兜底→人工确认。路由输入不含人工 Gold 标签与 task_type；'
                 'task_type 仅用于分层统计。'),
        'document_profile': profile,
        'thresholds': {'min_top_score': MIN_TOP_SCORE, 'min_margin': MIN_MARGIN},
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
        'batching_limits': {'claims': MAX_CLAIMS_PER_BATCH, 'facts': MAX_FACTS_PER_BATCH},
        'pricing': {'input_cny_per_million': INPUT_RATE, 'output_cny_per_million': OUTPUT_RATE},
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print()
    print(f'→ {DEST.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
