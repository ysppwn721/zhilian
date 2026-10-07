"""P0：自动任务识别 + 确定性路由（不读 task_type 标签）。

为什么必须做
------------
上一轮的「单值规则 100% + 增长修正版 BERT 100%」是**研究上界**：
评测脚本按 Gold 的 `task_type` 字段选择走哪条路。产品里没有这个字段，
必须从论断文本自动判定任务类型。否则 100% 不可复现、不可交付。

本脚本实现并度量三件事
----------------------
1. 自动任务识别：只用 claim_text 判定 quote / growth / abstain
2. 自动路由：quote → 纯规则（字段匹配）；growth → 模型候选 + 规则核验
3. 报告误路由率、规则覆盖率、拒答率，以及自动路由与上界的差值

规则核验（增长题）
------------------
模型给出 {本期, 上期} 两个来源后，用确定性规则校验：
  - 两个来源的 metric 必须一致
  - period 必须一个是本期、一个是上期
  - 若论断含百分比，用 (本期-上期)/|上期| 校验符号与量级
校验不过则拒答，不输出。

用法：
    python packaging/route_auto_taskid.py --device cpu
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / '答辩评测'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'
DATA = GD / 'human_gold_20261004_shuffled.jsonl'
OUT = GD / 'auto_taskid_route_report.json'

# ---- 增长/对比类标志（判定 growth_set）----
GROWTH_MARK = re.compile(
    r'同比|较上年|较上期|与上年同期|增减|变动|增长|下降|上升|减少|增加|增幅|降幅|'
    r'提高|降低|百分点|变化率')
PCT = re.compile(r'\d+(?:\.\d+)?\s*%')
# ---- 期间标志 ----
PRI_MARK = re.compile(r'上一报告期|上期|上年同期|上年度|去年同期|上年')
CUR_MARK = re.compile(r'本报告期|本期|报告期内|报告期|本年度|当期')
# ---- 指标同义映射（只做已知同义，不做自由等义）----
ALIAS = {
    '营收': '营业收入', '销售收入': '营业收入', '营业总收入': '营业收入', '销售金额': '营业收入',
    '归母净利润': '归属于上市公司股东的净利润', '净利': '净利润',
    '归母净资产': '归属于上市公司股东的净资产',
    '销售开支': '销售费用', '管理开支': '管理费用', '研发开支': '研发费用',
    '经营活动净现金流': '经营活动产生的现金流量净额',
}
SCOPE_PREF = ['合并', '母公司', '分部']


def detect_task(claim: str) -> str:
    """自动判定任务类型：growth / quote / unclear。"""
    if GROWTH_MARK.search(claim) or PCT.search(claim):
        return 'growth'
    if PRI_MARK.search(claim) or CUR_MARK.search(claim):
        return 'quote'
    return 'unclear'


def detect_period(claim: str) -> str | None:
    if PRI_MARK.search(claim):
        return '上期'
    if CUR_MARK.search(claim):
        return '本期'
    return None


def detect_metric(claim: str, candidates: list[dict]) -> tuple[str | None, str]:
    """从论断文本里定位指标名。返回 (metric, 依据)。"""
    names = sorted({c['metric'] for c in candidates}, key=len, reverse=True)
    for m in names:
        if m and m in claim:
            return m, 'exact'
    for alias, full in ALIAS.items():
        if alias in claim and any(c['metric'] == full for c in candidates):
            return full, f'alias:{alias}'
    # 去掉常见修饰后做包含匹配（如「扣除…后的营业收入」）
    for m in names:
        core = m.replace('归属于上市公司股东的', '').replace('（元）', '').strip()
        if len(core) >= 3 and core in claim:
            return m, 'core'
    return None, 'none'


def rule_quote(claim: str, cands: list[dict]) -> tuple[set[str], str]:
    """单值题规则：指标 + 期间 + 口径偏好。返回 (预测 fact_id 集合, 状态)。"""
    want_p = detect_period(claim)
    want_m, how = detect_metric(claim, cands)
    if want_m is None or want_p is None:
        return set(), f'abstain(missing:{how}/{want_p})'
    pool = [c for c in cands if c['metric'] == want_m and c['period'] == want_p]
    if not pool:
        return set(), 'abstain(no_candidate)'
    for pref in SCOPE_PREF:
        hit = [c for c in pool if c.get('scope') == pref]
        if hit:
            pool = hit
            break
    if len(pool) > 1:
        # 多候选且无法定序 → 拒答（交本地模型/人工）
        return set(), f'escalate({len(pool)}_candidates)'
    return {pool[0]['fact_id']}, 'rule'


def verify_growth(claim: str, picked: list[dict]) -> tuple[bool, str]:
    """增长题规则核验：两来源 metric 一致、期间互补，含百分比时校符号量级。"""
    if len(picked) != 2:
        return False, f'reject(k={len(picked)})'
    a, b = picked
    if a['metric'] != b['metric']:
        return False, 'reject(metric_mismatch)'
    if {a['period'], b['period']} != {'本期', '上期'}:
        return False, f"reject(period={a['period']},{b['period']})"
    mpct = PCT.search(claim)
    if mpct:
        cur = next((c for c in picked if c['period'] == '本期'), None)
        pri = next((c for c in picked if c['period'] == '上期'), None)
        cv, pv = cur.get('fact_value'), pri.get('fact_value')
        if isinstance(cv, (int, float)) and isinstance(pv, (int, float)) and pv:
            calc = (cv - pv) / abs(pv) * 100
            stated = float(mpct.group(0).rstrip('%'))
            if abs(abs(calc) - abs(stated)) > max(1.0, abs(stated) * 0.15):
                return False, f'reject(value_mismatch calc={calc:.2f} stated={stated})'
    return True, 'verified'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--no-model', action='store_true', help='只测规则与任务识别，不加载模型')
    ap.add_argument('--dataset', type=Path, default=DATA)
    ap.add_argument('--output', type=Path, default=OUT)
    args = ap.parse_args()

    rows = [json.loads(l) for l in args.dataset.read_text(encoding='utf-8').splitlines() if l.strip()]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r['claim_id']].append(r)
    for cid in grouped:
        grouped[cid].sort(key=lambda r: r['candidate_position'])
    qids = sorted(grouped)
    print(f'打乱集 {len(qids)} 题（自动任务识别，不读 task_type）')
    print()

    # ---------- 1) 自动任务识别准确率 ----------
    print('=== 1) 自动任务识别（对照 Gold task_type）===')
    conf = Counter()
    for cid in qids:
        gold_t = grouped[cid][0]['task_type']
        gmap = {'quote_current': 'quote', 'quote_prior': 'quote', 'growth_set': 'growth'}
        pred_t = detect_task(grouped[cid][0]['claim_text'])
        conf[(gmap[gold_t], pred_t)] += 1
    tot = sum(conf.values())
    correct = sum(v for (g, p), v in conf.items() if g == p)
    print(f'  准确率 {correct}/{tot} = {correct/tot*100:.2f}%')
    for (g, p), v in sorted(conf.items()):
        print(f'    gold={g:<7} 预测={p:<8} {v:>4}')
    print()

    # ---------- 2) 规则在单值题上的覆盖与拒答 ----------
    print('=== 2) 单值题：规则覆盖 / 升级 / 拒答 ===')
    stat = Counter()
    rule_hit = rule_total = 0
    rule_preds: dict[str, set[str]] = {}
    for cid in qids:
        g = grouped[cid]
        claim = g[0]['claim_text']
        if detect_task(claim) != 'quote':
            continue
        rule_total += 1
        pred, status = rule_quote(claim, g)
        stat[status.split('(')[0]] += 1
        rule_preds[cid] = pred
        gold = {c['fact_id'] for c in g if c['label'] == 1}
        if pred == gold:
            rule_hit += 1
    print(f'  被识别为单值题的: {rule_total}')
    for k, v in stat.most_common():
        print(f'    {k:10} {v:>4}')
    print(f'  规则直接答对: {rule_hit}/{rule_total} = {rule_hit/max(1,rule_total)*100:.2f}%')
    print()

    if args.no_model:
        return 0

    # ---------- 3) 增长题：模型候选 + 规则核验 ----------
    import evaluate_repaired_annual_benchmark as ev
    print('=== 3) 增长题：年报修正版 BERT 候选 + 规则核验 ===')
    scores = ev.score_rows(rows, 'annual_repaired_v1', batch_size=args.batch_size, device=args.device)
    sm = {(r['claim_id'], r['fact_id']): float(v) for r, v in zip(rows, scores)}

    g_total = g_model_ok = g_verified_ok = g_rejected = 0
    rej_reasons = Counter()
    for cid in qids:
        g = grouped[cid]
        claim = g[0]['claim_text']
        if detect_task(claim) != 'growth':
            continue
        g_total += 1
        gold = {c['fact_id'] for c in g if c['label'] == 1}
        ordered = sorted(g, key=lambda r: (-sm.get((cid, r['fact_id']), 0.0), r['candidate_position']))
        top2 = ordered[:2]
        if {c['fact_id'] for c in top2} == gold:
            g_model_ok += 1
        ok, why = verify_growth(claim, top2)
        if ok and {c['fact_id'] for c in top2} == gold:
            g_verified_ok += 1
        if not ok:
            g_rejected += 1
            rej_reasons[why.split('(')[0]] += 1
    print(f'  被识别为增长题的: {g_total}')
    print(f'  模型 Top-2 集合正确: {g_model_ok}/{g_total} = {g_model_ok/max(1,g_total)*100:.2f}%')
    print(f'  经规则核验后正确  : {g_verified_ok}/{g_total} = {g_verified_ok/max(1,g_total)*100:.2f}%')
    print(f'  规则核验拒答      : {g_rejected}  {dict(rej_reasons)}')
    print()

    # ---------- 4) 自动路由整体 ----------
    print('=== 4) 自动路由整体（vs 上界）===')
    ok = 0
    for cid in qids:
        g = grouped[cid]
        gold = {c['fact_id'] for c in g if c['label'] == 1}
        if cid in rule_preds and rule_preds[cid]:
            ok += int(rule_preds[cid] == gold)
        else:
            ordered = sorted(g, key=lambda r: (-sm.get((cid, r['fact_id']), 0.0), r['candidate_position']))
            top = ordered[:len(gold)]
            ok += int({c['fact_id'] for c in top} == gold)
    print(f'  自动路由集合精确匹配: {ok}/{len(qids)} = {ok/len(qids)*100:.2f}%')
    print(f'  上界（读 Gold task_type）: 见上一轮报告')

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        'dataset': str(args.dataset),
        'auto_taskid_accuracy': round(correct / tot, 4),
        'auto_taskid_confusion': {f'{g}->{p}': v for (g, p), v in conf.items()},
        'quote_tasks_detected': rule_total,
        'rule_status': dict(stat),
        'rule_direct_accuracy': round(rule_hit / max(1, rule_total), 4),
        'growth_tasks_detected': g_total,
        'growth_model_top2': round(g_model_ok / max(1, g_total), 4),
        'growth_after_verification': round(g_verified_ok / max(1, g_total), 4),
        'growth_verification_rejects': dict(rej_reasons),
        'auto_route_overall': round(ok / len(qids), 4),
        'note': '不读 task_type；自动任务识别 + 规则优先路由。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'→ {args.output.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
