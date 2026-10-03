"""第 3 步：构造完整的难负例池（错指标 / 错期间 / 错口径）。

为什么需要重建
--------------
v2 构造器的负例池只取了「同一张表内的其他指标行」，实测产出：
    wrong_metric 138 条
    wrong_scope    0 条   ← 因为口径几乎全是"未标明"，同表内分不出不同 scope
    wrong_period   0 条   ← 因为抽取器每个指标在中只产出一条事实，没有第二个期间

修正思路（利用年报的天然结构，而不是凭空造）：
  错指标 wrong_metric : 同表内其他指标行（保持原逻辑）
  错口径 wrong_scope  : **跨表**取同一指标——年报里同一指标会出现在
                        「主要会计数据」「分行业/分产品」「母公司」等多张表，
                        这些表的口径不同，是天然的错口径负例
  错期间 wrong_period : 同一指标 + 同一张表内的**其他年份列**
                        （三年表有 2021/2022 列；增长类论断指向本期，
                         指向其他年份即错期间）

输出：
  negatives_v1.jsonl  每条 = {claim_id, fact_id, label, negative_kind, ...}
  正例也一并输出（label=1），便于直接喂给训练脚本。
"""
from __future__ import annotations

import argparse
import hashlib
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
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')
YEAR_PAT = re.compile(r'(20\d{2})\s*年?')


def canon(x):
    if x is None:
        return None
    x = str(x).replace(',', '').replace('−', '-').replace('－', '-')
    neg = x.startswith('(') and x.endswith(')')
    x = x.strip('()')
    try:
        v = round(float(x.rstrip('%')), 2)
    except ValueError:
        return None
    return -v if neg else v


def fact_text(rec: dict) -> str:
    """事实文本：与 llm.suggest_links / reranker 的既有格式保持一致。"""
    return (f"subject={rec.get('subject') or '公司'}；metric={rec['metric']}；"
            f"period={rec['period']}；unit={rec.get('unit') or ''}；"
            f"scope={rec.get('scope_confirmed') or rec.get('scope') or ''}")


def signature(rec: dict) -> tuple:
    """(指标, 期间, 口径) 三元组——负例必须与正例在此三元组上**至少一维不同**，
    否则事实文本与正例完全一致，模型学不到任何区分（实测有这类退化负例）。
    """
    return (rec.get('metric'), rec.get('period'),
            rec.get('scope_confirmed') or rec.get('scope'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, default=ROOT / '答辩评测' / 'annual_reports_final')
    ap.add_argument('--neg-per-positive', type=int, default=4,
                    help='每条正例最多采多少条负例（按类型配额）')
    args = ap.parse_args()

    d = args.corpus
    # 以**已准入**的候选为基准，而不是原始候选。
    # 原始 candidates_v2.jsonl 含未过口径/三值/单位门槛的记录（1215 条），
    # 用它会为正例不存在的记录造负例，训练时对不上（实测 1215 vs 1029）。
    admitted_path = d / 'training_candidates.jsonl'
    if admitted_path.is_file():
        usable = [json.loads(l) for l in admitted_path.read_text(encoding='utf-8').splitlines() if l.strip()]
        src_name = admitted_path.name
    else:
        cs_all = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
        usable = [c for c in cs_all if c['claim_kind'] in ('growth', 'quote')]
        src_name = 'candidates_v2.jsonl'
    print(f'负例基准：{src_name}（{len(usable)} 条正例）')

    # 口径以准入后的 scope_confirmed 为准（原始 scope 多为"未标明"）
    for c in usable:
        if c.get('scope_confirmed'):
            c['scope'] = c['scope_confirmed']
    # 跨表索引仍需全部候选（用来找同一指标在别张表/别的口径下的出现）
    cs = [json.loads(l) for l in (d / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    for c in cs:
        if c.get('scope_confirmed'):
            c['scope'] = c['scope_confirmed']

    # 按 (公司, 指标) 建索引，用于跨表取错口径
    by_company_metric: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cs:
        by_company_metric[(c['company'], c['metric'])].append(c)

    pairs: list[dict] = []
    stats = Counter()

    for c in usable:
        claim_id = f"{c['company']}-{c['fact_page']}-{c['metric'][:18]}-{c['claim_kind']}"
        # ---- 正例 ----
        pos = {
            'claim_id': claim_id,
            'claim_text': c['claim_text'],
            'claim_page': c['claim_page'],
            'fact_id': c['metric'],
            'fact_text': fact_text(c),
            'fact_page': c['fact_page'],
            'fact_value': c['value'],
            'label': 1,
            'source_set': c['source_set'],
            'negative_kind': 'positive',
            'company': c['company'],
            'scope': c['scope'],
            'unit': c['unit'],
            'three_value_check': c['three_value_check'],
        }
        pairs.append(pos)
        stats['positive'] += 1
        # 增长率类需要第二来源（上期事实）
        if c['claim_kind'] == 'growth':
            pairs.append({**pos, 'fact_id': c['metric'] + '@上期',
                          'fact_text': (f"subject=公司；metric={c['metric']}；period=上期；"
                                        f"unit={c['unit'] or ''}；scope={c['scope'] or ''}"),
                          'fact_value': c['fact_prior_value'],
                          'negative_kind': 'positive', 'label': 1})
            stats['positive_prior_slot'] += 1

        # ---- 错指标：同表内其他候选 ----
        # 上限由「同表其他事实」决定（实测 485 张有正例的表内共 3692 条其他事实，
        # 平均 7.6 条/表），故每例最多取 6 条，并强制与正例在 (指标,期间,口径) 上
        # 至少一维不同——否则事实文本与正例一致，模型学不到区分。
        pos_sig = signature(c)
        seen_sig = {pos_sig}
        scope_picked = 0
        same_table = [x for x in cs
                      if x['company'] == c['company'] and x['fact_page'] == c['fact_page']
                      and x['metric'] != c['metric']]
        picked = 0
        for x in same_table:
            if picked >= 6:
                break
            sig = signature(x)
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            pairs.append({
                'claim_id': claim_id, 'claim_text': c['claim_text'], 'claim_page': c['claim_page'],
                'fact_id': x['metric'], 'fact_text': fact_text(x), 'fact_page': x['fact_page'],
                'fact_value': x['value'], 'label': 0, 'negative_kind': 'wrong_metric',
                'company': c['company'], 'scope': x.get('scope_confirmed') or x['scope'],
                'unit': x['unit'],
            })
            picked += 1
            stats['wrong_metric'] += 1

        # ---- 错期间：同一指标在**其他年份列**的真实数值 ----
        other_years = c.get('other_years') or {}
        for year, val in list(other_years.items())[:3]:
            if val is None or val == c['value']:
                continue
            pairs.append({
                'claim_id': claim_id, 'claim_text': c['claim_text'], 'claim_page': c['claim_page'],
                'fact_id': f"{c['metric']}@{year}", 'fact_text': fact_text({**c, 'period': f'{year}年'}),
                'fact_page': c['fact_page'], 'fact_value': val, 'label': 0,
                'negative_kind': 'wrong_period',
                'company': c['company'], 'scope': c.get('scope_confirmed') or c['scope'],
                'unit': c['unit'],
                'note': f"同表 {year} 年列：{val}",
            })
            stats['wrong_period'] += 1

        # ---- 错口径：同一指标在别张表出现，且口径确实不同 ----
        # 必须校验口径真的不同。实测有退化情形：另一张表的 scope 也是「未标明」，
        # 事实文本与正例完全一致，模型学不到任何区分。
        for x in by_company_metric[(c['company'], c['metric'])]:
            if scope_picked >= 2:
                break
            if x is c:
                continue
            if (x.get('caption') or '')[:20] == (c.get('caption') or '')[:20]:
                continue
            x_scope = x.get('scope_confirmed') or x.get('scope')
            c_scope = c.get('scope_confirmed') or c.get('scope')
            if not x_scope or x_scope == c_scope:
                stats['wrong_scope_skip_degenerate'] += 1
                continue
            pairs.append({
                'claim_id': claim_id, 'claim_text': c['claim_text'], 'claim_page': c['claim_page'],
                'fact_id': f"{x['metric']}@{x['fact_page']}页", 'fact_text': fact_text(x),
                'fact_page': x['fact_page'], 'fact_value': x['value'], 'label': 0,
                'negative_kind': 'wrong_scope',
                'company': c['company'], 'scope': x_scope, 'unit': x['unit'],
                'note': f"另一张表（{c_scope} → {x_scope}）：{(x.get('caption') or '')[:36]}",
            })
            stats['wrong_scope'] += 1
            scope_picked += 1

    # 去重：同一 (claim_id, fact_text, label) 只保留一条。
    # 实测有 24 条重复（同一负例被"跨表取指标"路径重复采到）。
    dedup: dict[tuple, dict] = {}
    for p in pairs:
        key = (p['claim_id'], p['fact_text'], p['label'])
        if key not in dedup:
            dedup[key] = p
    dropped = len(pairs) - len(dedup)
    pairs = list(dedup.values())

    out = d / 'negatives_v1.jsonl'
    out.write_text('\n'.join(json.dumps(p, ensure_ascii=False) for p in pairs), encoding='utf-8')

    print(f'=== 难负例池重建 ===')
    print(f'  总条目 {len(pairs)}' + (f'（去重丢弃 {dropped} 条重复）' if dropped else ''))
    for k in ('positive', 'positive_prior_slot', 'wrong_metric', 'wrong_scope', 'wrong_period'):
        print(f'  {k:20} {stats[k]:>5}')
    print()
    n_pos = stats['positive']
    n_neg = stats['wrong_metric'] + stats['wrong_scope'] + stats['wrong_period']
    print(f'  正例 {n_pos} 条（另 {stats["positive_prior_slot"]} 个上期来源槽）')
    print(f'  负例 {n_neg} 条 → 每条正例平均 {n_neg/max(1,n_pos):.1f} 条')
    print(f'  目标 {args.neg_per_positive} 负例/正例 = {n_pos*args.neg_per_positive} 条 '
          f'{"✓ 达标" if n_neg >= n_pos*args.neg_per_positive else "✗ 不足"}')
    print()
    print(f'  仍缺负例的正例: {sum(1 for p in pairs if p["negative_kind"]=="positive" and p["claim_id"] not in {q["claim_id"] for q in pairs if q["label"]==0})} 条')
    print(f'\n→ {out.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
