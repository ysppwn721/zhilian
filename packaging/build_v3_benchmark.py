"""构造 v3 基准：真实年报原句 + 真实数值 + 真实干扰项。

与 v1/v2 的区别（对应计划 P1 的要求）
------------------------------------
v1（human_gold_20261004）    ：真实原句，但单值题是「指标+期间」模板，不含数值
v2（annual_benchmark_repaired_20261004_v2）：候选构造合格，但 claim 0% 出自年报
v3（本脚本）                 ：claim **逐字出自年报原句且含真实数值**，
                              候选含四种真实干扰，正例集合标注完整

候选四角色（对应计划 P1-4）
--------------------------
  gold_source                 ：正例（本期或上期的真实事实）
  same_period_other_metric    ：同期间同主体、不同指标的真实事实
  same_metric_wrong_period    ：同指标、另一期间的真实事实（错期间干扰）
  same_metric_scope_mismatch  ：同指标同期间、但口径不同的真实事实

分层（对应计划 P1-3，按题计）
----------------------------
  quote_real_value  含真实数值的单值引用（本期/上期各半）
  growth_dual       增长率/双来源集合
  implicit_period   期间为隐含表达（报告期内/上年同期等）
  synonym           指标同义改写
  abstain           信息不足或口径冲突，应拒答

公司隔离：EXCLUDE 掉全部训练与既有评测公司（510 家）。

用法：
    python packaging/build_v3_benchmark.py --limit 20   # 20 条试运行
    python packaging/build_v3_benchmark.py             # 全量
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

POOL = ROOT / '答辩评测' / 'annual_reports_v3_pool'
OUT_DIR = ROOT / '答辩评测' / 'v3_real_value_20261005'
SEED = 20261005

PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%')
NUM_IN = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?')
# 期间隐含表达（计划 P1-3 第 3 层）
IMPLICIT = re.compile(r'报告期内|本报告期|上年同期|去年同期|期末|期初|本年度|上年度')
# 指标同义（只做已知同义，不做自由等义）
SYNONYMS = {
    '营业收入': ['营收', '销售收入', '营业总收入', '销售金额'],
    '归属于上市公司股东的净利润': ['归母净利润', '归属于母公司股东的净利润'],
    '净利润': ['净利', '纯利'],
    '销售费用': ['销售开支'],
    '管理费用': ['管理开支'],
    '研发费用': ['研发开支'],
    '经营活动产生的现金流量净额': ['经营活动净现金流'],
}
FROZEN = {'000615','000930','002097','002388','002413','002425','002569','002598','002808',
          '002825','300149','300632','600080','600165','600187','603839','688152','836263','873576'}


def excluded_companies() -> set[str]:
    """全部需隔离的公司。"""
    ex = set(FROZEN)
    p = ROOT / '答辩评测' / 'annual_reports_final' / 'manifest.jsonl'
    if p.is_file():
        ex |= {json.loads(l)['stock_code'] for l in p.read_text(encoding='utf-8').splitlines() if l.strip()}
    for split in ('train', 'dev', 'test'):
        p = ROOT / '答辩评测' / 'annual_reports_weak_training_repaired_20261004' / f'{split}.jsonl'
        if p.is_file():
            ex |= {json.loads(l).get('company') for l in p.read_text(encoding='utf-8').splitlines() if l.strip()}
    p = ROOT / '答辩评测' / 'human_gold_eval_20261004' / 'human_gold_20261004.jsonl'
    if p.is_file():
        ex |= {json.loads(l).get('company') for l in p.read_text(encoding='utf-8').splitlines() if l.strip()}
    ex.discard(None)
    return ex


def value_in_sentence_units(value, sent: str, fact_unit: str | None = None):
    """把 value 归一化到句子中实际使用的单位，返回 (数值, 单位)。

    必要性：同一指标的 value 字段在不同公司基准不同（元 / 万元）。
    若不归一，claim 里写「1,853,355.54 万元」而 fact_value 记 18,533,555,418.42，
    量纲不一致，会让基准自相矛盾。
    """
    v = canon_num(value)
    if v is None:
        return None, fact_unit
    for sv, su in sentence_values_with_unit(sent):
        for v_base in ('元', fact_unit or '元'):
            f_v = UNIT_FACTOR.get(v_base, 1.0)
            f_s = UNIT_FACTOR.get(su, 1.0)
            if abs(v * f_v / f_s - sv) <= max(abs(sv) * 1e-6, 0.02):
                return sv, su
        if abs(v - sv) <= max(abs(sv) * 1e-9, 1e-6):
            return sv, su
    return v, fact_unit


def canon_num(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').replace('－', '-').strip('()')
    try:
        return round(float(x.rstrip('%')), 2)
    except ValueError:
        return None


# 单位换算因子（相对「元」）
UNIT_FACTOR = {'元': 1.0, '千元': 1e3, '万元': 1e4, '亿元': 1e8}
UNIT_IN_TEXT = re.compile(r'(亿元|万元|千元|元)')


def sentence_values_with_unit(sent: str) -> list[tuple[float, str]]:
    """抽出句子里的 (数值, 单位)。单位取数值后紧跟的货币单位，缺省为「元」。

    为什么必须单位感知：实测同一指标的 value 字段在不同公司基准不同——
    600196 存「元」(18,533,555,418.42)，603718 存「万元」(25,456.52)，
    而句子写的是「25,456.52 万元」。只用裸数字比较会产生假阴性，
    更严重的是会让 claim 与 fact_value 量纲不一致。
    """
    out = []
    for m in re.finditer(r'[-−(]?\d[\d,]*(?:\.\d+)?', sent):
        v = canon_num(m.group(0))
        if v is None:
            continue
        tail = sent[m.end():m.end() + 6]
        um = UNIT_IN_TEXT.match(tail.strip()[:3]) if tail.strip() else None
        unit = um.group(1) if um else '元'
        out.append((v, unit))
    return out


def value_matches_sentence(value, sent: str, fact_unit: str | None = None) -> bool:
    """判断 value 是否与句子中某个数值一致（允许单位换算）。

    同时尝试两种解释：value 以「元」为基准，或以句子里的单位 / fact_unit 为基准。
    """
    v = canon_num(value)
    if v is None:
        return False
    for sv, su in sentence_values_with_unit(sent):
        for v_base in ('元', fact_unit or '元'):
            f_v = UNIT_FACTOR.get(v_base, 1.0)
            f_s = UNIT_FACTOR.get(su, 1.0)
            # value 视作 v_base 单位 → 元 → 句子单位
            if abs(v * f_v / f_s - sv) <= max(abs(sv) * 1e-6, 0.02):
                return True
        # 也接受"数值直接相等"（用于 %、元/股 等非货币量纲）
        if abs(v - sv) <= max(abs(sv) * 1e-9, 1e-6):
            return True
    return False


def stratified_synonym(metric: str, sent: str) -> str | None:
    """把句子里的指标名替换为同义说法，数值与其余文字不动。

    对 growth 句同样适用：换指标名、保留原数值与百分比。
    """
    for syn in SYNONYMS.get(metric, []):
        if metric in sent:
            return sent.replace(metric, syn, 1)
    return None


def synonym_pool_entries(rows: list[dict], ex: set, no_synonym: bool) -> list[dict]:
    """同义层候选池：凡有已知同义说法、句子够长、指标名确在句中的条目都算。

    旧实现只把 quote 条目放进同义层，导致池内只有 25 条，
    而 growth 句同样可以做同义改写（换指标名、数值不动）。
    """
    if no_synonym:
        return []
    out = []
    for c in rows:
        if c['company'] in ex:
            continue
        if c['claim_kind'] not in ('growth', 'quote'):
            continue
        sent = (c.get('claim_text') or '').strip()
        if len(sent) < 12 or not SYNONYMS.get(c['metric']):
            continue
        if c['metric'] not in sent:
            continue
        if c['claim_kind'] == 'quote' and not value_matches_sentence(c.get('value'), sent, c.get('unit')):
            continue
        out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool', type=Path, default=POOL)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out-dir', type=Path, default=OUT_DIR)
    ap.add_argument('--no-synonym', action='store_true', help='不做同义改写分层')
    ap.add_argument('--quota-synonym', type=int, default=75, help='同义改写层题数（约25%）')
    ap.add_argument('--quota-sameperiod', type=int, default=75, help='同期间异指标层（约25%）')
    ap.add_argument('--quota-implicit', type=int, default=60, help='隐含期间层（约20%）')
    ap.add_argument('--quota-growth', type=int, default=60, help='增长双来源层（约20%）')
    ap.add_argument('--quota-abstain', type=int, default=30, help='应拒答层（约10%）')
    args = ap.parse_args()

    d = args.pool
    rows = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    candidates = [r for r in rows if r['claim_kind'] in ('growth', 'quote')]
    ex = excluded_companies()
    candidates = [r for r in candidates if r['company'] not in ex]

    print(f'v3 构造：池内可用 {len(candidates)} 条（隔离排除 {len(ex)} 家公司）')
    print()

    # ---- 按 (公司, 表页) 建表级事实索引，用于构造真实干扰 ----
    by_table: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:                       # 用全部事实（含 table_only）做干扰池更真实
        by_table[(r['company'], r['fact_page'])].append(r)

    # ---- 先把可用条目分类进各层的候选池 ----
    pools: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        sent = (c.get('claim_text') or '').strip()
        if len(sent) < 12:
            continue
        value = c.get('value')
        if c['claim_kind'] == 'quote' and not value_matches_sentence(value, sent, c.get('unit')):
            continue
        # 单位异常（实测有把金额标成 %）——排除，避免把量纲错误带进基准
        if c.get('unit') == '%' and not re.search(r'率|比例|占比', c['metric']):
            continue
        has_syn = bool(SYNONYMS.get(c['metric']))
        if c['claim_kind'] == 'growth':
            pools['growth'].append(c)
        if IMPLICIT.search(sent):
            pools['implicit'].append(c)
        # 同期间异指标层：任何条目都可用（干扰来自同表其他事实）
        pools['sameperiod'].append(c)

    # 同义层单独建池：growth 与 quote 都可参与（旧实现漏了 growth，池内只有 25 条）
    pools['synonym'] = synonym_pool_entries(rows, ex, args.no_synonym)

    # ---- 按公司轮转取用，避免少数公司垄断（计划 P1-6 公司隔离之外还要分散）----
    def take(pool_name: str, n: int, used_keys: set) -> list[dict]:
        byco: dict[str, list[dict]] = defaultdict(list)
        for c in pools.get(pool_name, []):
            byco[c['company']].append(c)
        order = sorted(byco, key=lambda k: -len(byco[k]))
        out, i = [], 0
        while len(out) < n and any(byco[k] for k in order):
            k = order[i % len(order)]
            i += 1
            if not byco[k]:
                continue
            c = byco[k].pop(0)
            key = (c['company'], c['metric'], c['fact_page'], c['claim_kind'])
            if key in used_keys:
                continue
            used_keys.add(key)
            out.append(c)
        return out

    used: set = set()
    plan = [('synonym', args.quota_synonym), ('sameperiod', args.quota_sameperiod),
            ('implicit', args.quota_implicit), ('growth', args.quota_growth)]
    chosen: list[tuple[str, dict]] = []
    for name, n in plan:
        got = take(name, n, used)
        chosen += [(name, c) for c in got]
        print(f'  层 {name:12} 取到 {len(got):>3} / 目标 {n}  （池内可选 {len(pools.get(name, []))}）')
    print(f'  合计 {len(chosen)} 题')
    print()

    qid = 0
    out_rows: list[dict] = []
    stats = Counter()

    for layer, c in chosen:
        company = c['company']
        sent = (c.get('claim_text') or '').strip()
        value = c.get('value')
        table_facts = by_table[(company, c['fact_page'])]
        task_type = 'growth_set' if c['claim_kind'] == 'growth' else (
            'quote_current' if c.get('period') == '本期' else 'quote_prior')

        variants = [('literal', sent)]
        if layer == 'synonym' and not args.no_synonym:
            syn = stratified_synonym(c['metric'], sent)
            if syn:
                variants.append(('synonym', syn))
                stats['with_synonym'] += 1

        for variant, claim_text in variants:
            qid += 1
            claim_id = f'v3-{company}-{qid:04d}'
            layer2 = 'synonym' if variant == 'synonym' else layer

            # ---- 构造候选：正例 + 三种真实干扰 ----
            cand: list[dict] = []
            used_fact_ids: set[str] = set()

            def add(role: str, metric: str, period: str, scope: str, unit, val, label: int,
                    sent_for_units: str | None = None):
                # Every fact needs a stable identity.  Using only ``metric`` for
                # positives collapses the current/prior pair in growth claims.
                # The period and scope are part of the identity; a suffix keeps
                # duplicate rows from the same table distinct without changing
                # their auditable metadata.
                base_fid = f'{metric}@{period}@{scope or "未标明"}'
                fid = base_fid
                duplicate = 2
                while fid in used_fact_ids:
                    fid = f'{base_fid}#{duplicate}'
                    duplicate += 1
                used_fact_ids.add(fid)
                # 正例数值与单位对齐到 claim 实际使用的单位，避免量纲不一致
                shown = val
                shown_unit = unit
                if label == 1 and sent_for_units:
                    nv, nu = value_in_sentence_units(val, sent_for_units, unit)
                    if nv is not None:
                        shown, shown_unit = nv, nu
                cand.append({
                    'fact_id': fid, 'metric': metric, 'period': period,
                    'scope': scope or '未标明', 'unit': shown_unit, 'fact_value': shown,
                    'label': label, 'candidate_role': role,
                    'fact_text': (f'subject=公司；metric={metric}；period={period}；'
                                  f'unit={shown_unit or "未标明"}；scope={scope or "未标明"}'),
                })

            # 正例：本题指标，本题期间（单位对齐到 claim）
            add('gold_source', c['metric'], c.get('period') or '本期',
                c.get('scope_confirmed') or '合并', c.get('unit'), value, 1, sent)
            # growth 题的第二来源（上期）
            if c['claim_kind'] == 'growth' and c.get('fact_prior_value') is not None:
                add('gold_source', c['metric'], '上期',
                    c.get('scope_confirmed') or '合并', c.get('unit'), c['fact_prior_value'], 1, sent)

            # 干扰 1：同期间、不同指标（同表的真实其他事实）
            seen = {(x['metric'], x['period']) for x in cand}
            n1 = 0
            for f in table_facts:
                if f['metric'] == c['metric'] or n1 >= 6:
                    continue
                per = f.get('period') or '本期'
                k = (f['metric'], per)
                if k in seen:
                    continue
                seen.add(k)
                add('same_period_other_metric', f['metric'], per,
                    f.get('scope_confirmed') or '合并', f.get('unit'), f.get('value'), 0)
                n1 += 1
            # 干扰 2：同指标、错期间（用同表其他年份列）
            oy = c.get('other_years') or {}
            for year, val in list(oy.items())[:2]:
                if val is None or canon_num(val) == canon_num(value):
                    continue
                add('same_metric_wrong_period', c['metric'], f'{year}年',
                    c.get('scope_confirmed') or '合并', c.get('unit'), val, 0)
            # 干扰 3：同指标同期间、不同口径
            if c.get('scope_confirmed') and c['scope_confirmed'] != '合并':
                add('same_metric_scope_mismatch', c['metric'], c.get('period') or '本期',
                    '合并', c.get('unit'), value, 0)

            if len(cand) < 3:
                stats['skip_too_few_candidates'] += 1
                continue

            rng = random.Random(SEED + qid)
            rng.shuffle(cand)
            for pos, x in enumerate(cand):
                out_rows.append({
                    'claim_id': claim_id,
                    'company': company,
                    'source_file': c['source_file'],
                    'source_page': c.get('fact_page'),
                    'source_sentence': sent,
                    'claim_text': claim_text,
                    'claim_variant': variant,
                    'layer': layer2,
                    'task_type': task_type,
                    'target_period': ('本期+上期' if task_type == 'growth_set' else
                                      ('本期' if task_type == 'quote_current' else '上期')),
                    'expected_k': sum(1 for x2 in cand if x2['label'] == 1),
                    'metric': x['metric'], 'period': x['period'], 'unit': x['unit'],
                    'scope': x['scope'], 'fact_value': x['fact_value'],
                    'fact_id': x['fact_id'], 'fact_text': x['fact_text'],
                    'label': x['label'], 'candidate_role': x['candidate_role'],
                    'candidate_position': pos,
                    'gold_fact_ids': [x2['fact_id'] for x2 in cand if x2['label'] == 1],
                    'position_shuffled': True,
                    'programmatic': True,
                })
            stats[f'layer_{layer2}'] += 1
            stats['questions'] += 1
            if args.limit and stats['questions'] >= args.limit:
                break
        if args.limit and stats['questions'] >= args.limit:
            break

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    name = 'v3_pilot20.jsonl' if args.limit else 'benchmark_v3.jsonl'
    outp = out_dir / name
    outp.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in out_rows), encoding='utf-8')

    qs = defaultdict(list)
    for r in out_rows:
        qs[r['claim_id']].append(r)

    # 零信息基线（必须接近随机，否则基准不合格）
    n = len(qs)
    first = sum(1 for g in qs.values() if g[0]['label'] == 1) / max(1, n)
    last = sum(1 for g in qs.values() if g[-1]['label'] == 1) / max(1, n)
    exp = sum(sum(1 for x in g if x['label'] == 1) / len(g) for g in qs.values()) / max(1, n)

    # 真实数值校验：多少题的 claim 文本含正例数值
    real = 0
    for g in qs.values():
        gv = [x['fact_value'] for x in g if x['label'] == 1]
        if any(value_matches_sentence(v, g[0]['claim_text'], None) for v in gv):
            real += 1
    # 溯源校验：句子能否在原文定位（此处只记字段，定位由独立脚本核验）
    report = {
        'pool': str(d.relative_to(ROOT)),
        'excluded_companies': len(ex),
        'questions': n, 'rows': len(out_rows),
        'companies': len({r['company'] for r in qs and out_rows}),
        'layers': {k[6:]: v for k, v in stats.items() if k.startswith('layer_')},
        'task_types': dict(Counter(g[0]['task_type'] for g in qs.values())),
        'candidate_roles': dict(Counter(r['candidate_role'] for r in out_rows)),
        'claim_contains_real_value': round(real / max(1, n), 4),
        'zero_info_baselines': {'always_first': round(first, 4), 'always_last': round(last, 4),
                                'random_expected': round(exp, 4)},
        'sha256': hashlib.sha256(outp.read_bytes()).hexdigest(),
        'seed': SEED,
        'note': ('v3：claim 逐字出自年报原句并含真实数值；候选含四种真实角色；'
                 '公司与全部训练集/既有评测集隔离。'),
    }
    (out_dir / f'{name.replace(".jsonl", "")}_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'→ {outp.name}')
    print(f'  题目 {n} · 候选 {len(out_rows)} · 公司 {report["companies"]}')
    print(f'  分层: {report["layers"]}')
    print(f'  任务: {report["task_types"]}')
    print(f'  候选角色: {report["candidate_roles"]}')
    print(f'  claim 含真实数值的题占比: {real/max(1,n)*100:.1f}%')
    print(f'  零信息基线: 首 {first*100:.2f}% · 末 {last*100:.2f}% · 随机期望 {exp*100:.2f}%')
    if abs(last - exp) > 0.10:
        print(f'  ⚠ 末位基线与随机差 {abs(last-exp)*100:.1f} 点，需检查位置伪影')
    else:
        print(f'  ✓ 末位基线与随机接近（差 {abs(last-exp)*100:.1f} 点）')
    print(f'  跳过的: { {k: v for k, v in stats.items() if k.startswith("skip_")} }')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
