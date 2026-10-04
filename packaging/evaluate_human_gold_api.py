"""纯 API 评测：deepseek-flash 在打乱版人工 Gold 集上做论断—事实关联。

任务形态
--------
题干是检索式指令（如「请核对公司本期披露的营业收入对应的事实来源。」），
候选是若干条事实（metric / period / unit / scope）。模型须指出正确的事实。

评分口径（与既有各模型一致，便于同表对比）
------------------------------------------
- 集合精确匹配：模型给出的 fact_id 集合 == 该题的正例集合，且无并列
- quote_current / quote_prior：期望 1 条
- growth_set：期望 2 条（本期来源 + 上期来源）

实现要点（都是踩过的坑）
------------------------
1. deepseek-flash 会输出 `reasoning_content`，**推理 token 计入 max_tokens**。
   实测 max_tokens=20 时 content 为空。故 max_tokens 取 4096。
2. 要求严格 JSON 输出，解析失败时退回正则抽取，再失败记 parse_error 不伪造结果。
3. 逐题独立调用（不做批量），避免跨题干扰；并发受控，失败重试 2 次。
4. 记录 token 与费用估算，写入报告。

用法：
    python packaging/evaluate_human_gold_api.py            # 全量 150 题
    python packaging/evaluate_human_gold_api.py --limit 5  # 先小样验证
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'
SRC = GD / 'human_gold_20261004_shuffled.jsonl'
ENV = ROOT / '答辩评测' / 'human_gold_api.env'
OUT = GD / 'human_gold_pure_api_20261004.jsonl'
REP = GD / 'human_gold_pure_api_report.json'


def load_env() -> dict:
    cfg = {}
    if ENV.is_file():
        for line in ENV.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg[k.strip()] = v.strip()
    for k in ('DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'ZHILIAN_LLM_BASE_URL',
              'ZHILIAN_INPUT_PRICE_PER_MILLION', 'ZHILIAN_OUTPUT_PRICE_PER_MILLION'):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


SYSTEM = (
    '你是年报事实核对助手。用户给出一条核对指令和若干候选事实。'
    '请判断哪些候选事实**正是该指令所指的来源**。\n'
    '规则：\n'
    '1. 必须同时匹配指标名与报告期。指令中的“本期/本报告期/报告期内”指本期，'
    '“上期/上年同期/上一报告期”指上期。\n'
    '2. **若指令陈述的是“变化/增长/下降/同比/较上年”这类对比关系**，'
    '则该论断同时依赖本期与上期的两条来源，两条都要选。\n'
    '3. 同一指标可能有多个口径（合并/母公司/分部）。优先选择“合并”口径；'
    '若只有其他口径，也可选。\n'
    '4. 指标名可有同义说法（如“营收”=“营业收入”、“净利”=“净利润”），'
    '但不得把不同指标当成同一指标。\n'
    '5. **只选指令真正指向的那个指标**，不要连带选择指令中顺带提到的其他指标。\n'
    '6. 只输出 JSON，不要解释。格式：{"fact_ids": ["<候选的 id>", ...]}\n'
    '7. fact_ids 只填你确认的候选 id，数量由证据决定（1 条或 2 条）。'
)


def build_prompt(claim_text: str, cands: list[dict]) -> str:
    lines = [f'核对指令：{claim_text}', '', '候选事实：']
    for c in cands:
        lines.append(
            f"- id={c['fact_id']} | 指标={c['metric']} | 报告期={c['period']} "
            f"| 单位={c.get('unit') or '未标明'} | 口径={c.get('scope') or '未标明'}")
    lines += ['', '请输出 JSON：{"fact_ids": [...]}']
    return '\n'.join(lines)


def call_api(cfg: dict, prompt: str, timeout: int = 180) -> tuple[dict, dict]:
    body = {
        'model': cfg.get('DEEPSEEK_MODEL', 'deepseek-flash'),
        'messages': [{'role': 'system', 'content': SYSTEM},
                     {'role': 'user', 'content': prompt}],
        'max_tokens': 4096,
        'temperature': 0,
    }
    req = urllib.request.Request(
        cfg.get('ZHILIAN_LLM_BASE_URL', 'https://api.deepseek.com') + '/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': f"Bearer {cfg['DEEPSEEK_API_KEY']}",
                 'Content-Type': 'application/json'},
        method='POST')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        return payload, {'ok': True, 'ms': int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        return {}, {'ok': False, 'ms': int((time.time() - t0) * 1000),
                    'error': f'HTTP {e.code}: {detail}'}
    except Exception as e:
        return {}, {'ok': False, 'ms': int((time.time() - t0) * 1000),
                    'error': f'{type(e).__name__}: {e}'}


def parse_ids(content: str, allowed: set[str]) -> tuple[list[str], str]:
    """解析模型输出中的 fact_ids。返回 (ids, 解析方式)。"""
    if not content:
        return [], 'empty'
    # 严格 JSON
    m = re.search(r'\{.*\}', content, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            ids = obj.get('fact_ids')
            if isinstance(ids, list):
                return [str(x).strip() for x in ids if str(x).strip() in allowed], 'json'
        except json.JSONDecodeError:
            pass
    # 退回：抽取候选 id 字面出现
    found = [a for a in allowed if a in content]
    # 长 id 优先（避免 "营业收入" 命中 "营业收入@上期" 的歧义）
    found.sort(key=len, reverse=True)
    return found[:3], 'fallback'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--dataset', type=Path, default=SRC)
    args = ap.parse_args()

    cfg = load_env()
    if not cfg.get('DEEPSEEK_API_KEY'):
        print('缺少 DEEPSEEK_API_KEY', file=sys.stderr)
        return 2

    rows = [json.loads(l) for l in args.dataset.read_text(encoding='utf-8').splitlines() if l.strip()]
    byq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byq[r['claim_id']].append(r)
    # 保持打乱后的位置顺序
    for cid in byq:
        byq[cid].sort(key=lambda r: r['candidate_position'])
    qids = sorted(byq)
    if args.limit:
        qids = qids[:args.limit]

    print(f'纯 API 评测：{len(qids)} 题 · 模型 {cfg.get("DEEPSEEK_MODEL")}')
    print(f'数据集：{args.dataset.name}')
    print()

    results, costs = [], Counter()
    t0 = time.time()

    def work(cid: str) -> dict:
        cands = byq[cid]
        allowed = {c['fact_id'] for c in cands}
        gold = {c['fact_id'] for c in cands if c['label'] == 1}
        prompt = build_prompt(cands[0]['claim_text'], cands)
        last = None
        for attempt in range(3):
            payload, meta = call_api(cfg, prompt)
            if meta.get('ok'):
                msg = (payload.get('choices') or [{}])[0].get('message', {}) or {}
                content = msg.get('content') or ''
                ids, how = parse_ids(content, allowed)
                u = payload.get('usage') or {}
                return {
                    'claim_id': cid, 'task_type': cands[0]['task_type'],
                    'company': cands[0]['company'],
                    'claim_text': cands[0]['claim_text'],
                    'n_candidates': len(cands), 'expected_k': cands[0].get('expected_k'),
                    'gold_fact_ids': sorted(gold),
                    'pred_fact_ids': ids,
                    'exact_match': set(ids) == gold,
                    'parse_mode': how,
                    'reasoning_chars': len(msg.get('reasoning_content') or ''),
                    'content_raw': content[:500],
                    'prompt_tokens': u.get('prompt_tokens', 0),
                    'completion_tokens': u.get('completion_tokens', 0),
                    'attempt': attempt + 1,
                }
            last = meta
            time.sleep(1.5 * (attempt + 1))
        return {'claim_id': cid, 'task_type': cands[0]['task_type'],
                'company': cands[0]['company'], 'claim_text': cands[0]['claim_text'],
                'n_candidates': len(cands), 'gold_fact_ids': sorted(gold),
                'pred_fact_ids': [], 'exact_match': False, 'parse_mode': 'request_failed',
                'error': (last or {}).get('error'), 'prompt_tokens': 0, 'completion_tokens': 0}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, cid): cid for cid in qids}
        done = 0
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 10 == 0 or done == len(qids):
                ok = sum(1 for r in results if r['exact_match'])
                print(f'  [{done}/{len(qids)}] 集合精确匹配 {ok} ({ok/done*100:.1f}%)', flush=True)

    results.sort(key=lambda r: r['claim_id'])
    OUT.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in results), encoding='utf-8')

    # 统计
    pt = sum(r.get('prompt_tokens', 0) for r in results)
    ct = sum(r.get('completion_tokens', 0) for r in results)
    ip = float(cfg.get('ZHILIAN_INPUT_PRICE_PER_MILLION', 1) or 1)
    op = float(cfg.get('ZHILIAN_OUTPUT_PRICE_PER_MILLION', 4) or 4)
    cost = pt / 1e6 * ip + ct / 1e6 * op

    by_task = defaultdict(lambda: {'n': 0, 'ok': 0})
    for r in results:
        k = r['task_type']
        by_task[k]['n'] += 1
        by_task[k]['ok'] += int(r['exact_match'])

    rep = {
        'experiment': 'human_gold_pure_api_20261004',
        'provider': 'DeepSeek', 'model': cfg.get('DEEPSEEK_MODEL'),
        'dataset': str(args.dataset.relative_to(ROOT)),
        'dataset_shuffled': 'position_shuffled' in (rows[0] if rows else {}),
        'questions': len(results),
        'set_exact_match_overall': round(sum(1 for r in results if r['exact_match']) / max(1, len(results)), 4),
        'by_task': {k: {'n': v['n'], 'exact_match': round(v['ok'] / v['n'], 4),
                        'quote_top1_accuracy': round(v['ok'] / v['n'], 4)}
                    for k, v in by_task.items()},
        'parse_modes': dict(Counter(r['parse_mode'] for r in results)),
        'failed_requests': sum(1 for r in results if r['parse_mode'] == 'request_failed'),
        'prompt_tokens': pt, 'completion_tokens': ct,
        'estimated_cost_cny': round(cost, 4),
        'elapsed_sec': round(time.time() - t0, 1),
        'note': ('纯 API 逐题独立调用；候选顺序已打乱（种子 20261004），'
                 '位置不再携带信息。评分为集合精确匹配。'),
    }
    REP.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')

    print()
    print('=' * 68)
    print(f'  题目            : {rep["questions"]}')
    print(f'  集合精确匹配    : {rep["set_exact_match_overall"]*100:.2f}%')
    for k, v in rep['by_task'].items():
        print(f'    {k:14} n={v["n"]:>3}  {v["exact_match"]*100:>6.2f}%')
    print(f'  解析方式        : {rep["parse_modes"]}')
    print(f'  失败请求        : {rep["failed_requests"]}')
    print(f'  token           : prompt {pt} · completion {ct}')
    print(f'  估算费用        : ¥{cost:.4f}')
    print(f'  用时            : {rep["elapsed_sec"]}s')
    print('=' * 68)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
