"""诊断零产出：417 家里 327 家为什么没产出候选。

漏斗（每关都会丢人）：
  A 主体本身：年报里根本定位不到那两类页
  B 有目标页但无带期间列的财务表
  C 有表但列角色判不出（本期/上期）
  D 有事实行但正文无锚点（table_only）
  E 有锚点但三值/口径/单位没过
  F 全通过 → 可训练

同时统计：table_only 与 abstain 的数量，判断"正文锚点召回"有多少提升空间。
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
D = ROOT / '答辩评测' / 'annual_reports_all'
NUM = re.compile(r'[-−(]?\d[\d,]*(?:\.\d+)?%?[)]?')


def main() -> int:
    import pymupdf

    manifest = [json.loads(l) for l in (D / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    cands = [json.loads(l) for l in (D / 'candidates_v2.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    train = [json.loads(l) for l in (D / 'training_candidates.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    stats = json.loads((D / 'candidates_v2_stats.json').read_text(encoding='utf-8'))

    by_co_kind = defaultdict(Counter)
    for c in cands:
        by_co_kind[c['company']][c['claim_kind']] += 1
    train_by_co = Counter(r['company'] for r in train)

    print('=== 漏斗：候选层（candidates_v2.jsonl，按 claim_kind）===')
    kinds = Counter(c['claim_kind'] for c in cands)
    total = sum(kinds.values())
    for k in ('growth', 'quote', 'abstain', 'table_only'):
        print(f'  {k:12} {kinds[k]:>5}  ({kinds[k]/max(1,total)*100:5.1f}%)')
    print(f'  {"合计":12} {total:>5}')
    print()
    print('=== 漏斗：从候选到可训练 ===')
    print(f'  候选总数        {total}')
    print(f'  可用(growth+quote) {kinds["growth"]+kinds["quote"]}')
    print(f'  口径已判定      {stats.get("tables", 0)} 张表 → 判定见 scope_decisions')
    print(f'  可训练          {len(train)}')
    dropped = (kinds['growth'] + kinds['quote']) - len(train)
    print(f'  在口径/三值/单位关丢失 {dropped}')
    print()

    # 按公司归类
    cats = Counter()
    detail = defaultdict(list)
    for r in manifest:
        code = r['stock_code']
        k = by_co_kind.get(code, Counter())
        if train_by_co.get(code):
            cats['F 产出可训练候选'] += 1
        elif k.get('growth', 0) + k.get('quote', 0) > 0:
            cats['E 有候选但未过准入'] += 1
        elif k.get('abstain', 0) > 0:
            cats['D2 有指标名但无数值锚点(abstain)'] += 1
        elif k.get('table_only', 0) > 0:
            cats['D1 只有表格事实，正文无锚点'] += 1
        else:
            cats['A/B/C 连事实行都没有'] += 1
            detail['nofact'].append(code)
    print('=== 按公司归类（417 家）===')
    for k, v in sorted(cats.items()):
        print(f'  {k:34} {v:>4}')
    print()

    # 深挖 A/B/C：到底是没目标页，还是有页面没表
    print('=== 深挖「连事实行都没有」的公司 ===')
    codes = detail['nofact'][:40]
    probe = Counter()
    for r in manifest:
        if r['stock_code'] not in codes:
            continue
        p = D / r['local_file']
        if not p.is_file():
            continue
        try:
            doc = pymupdf.open(p)
            pages = [(i + 1, (pg.get_text('text') or '')) for i, pg in enumerate(doc)]
            doc.close()
        except Exception:
            continue
        sec2 = sum(1 for _, t in pages if re.search(r'第\s*二\s*节|主要会计数据和财务指标', t[:600]))
        sec3 = sum(1 for _, t in pages if re.search(r'第\s*三\s*节|管理层讨论与分析|经营情况讨论与分析', t[:600]))
        has_cur = sum(1 for _, t in pages if re.search(r'本期|期末余额|本报告期', t))
        has_pri = sum(1 for _, t in pages if re.search(r'上期|上年同期|期初余额', t))
        if sec2 or sec3:
            probe['有目标页标题'] += 1
        else:
            probe['连目标页标题都没有'] += 1
        if has_cur and has_pri:
            probe['页面文本里同时有本期/上期字样'] += 1
        else:
            probe['页面里缺本期或上期字样'] += 1
    for k, v in probe.most_common():
        print(f'  {k:30} {v:>4} / {len(codes)} 抽样')
    print()

    # table_only 的可挖掘性：这些表里有没有"正文提到指标名但数值不同"的情况
    print('=== 提升空间估算 ===')
    print(f'  table_only {kinds["table_only"]} 条：这些事实正文从未提及，')
    print(f'    但它们所在表的**表头行**本身可能含指标名，属于可挖掘的"零锚点"池。')
    print(f'  abstain   {kinds["abstain"]} 条：正文出现指标名但数值对不上。')
    print(f'    其中一部分是"行业数据/另一口径数值"——天然适合做**弃答样本**，')
    print(f'    按任务书要求应保留，不应丢弃。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
