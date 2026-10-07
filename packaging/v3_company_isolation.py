"""v3 公司隔离核查：确定哪些公司可用于 v3 基准。

v3 必须与以下全部隔离：
  - annual_reports_final（我的 491 份语料，1029 条候选的来源）
  - annual_reports_weak_training_repaired_20261004（年报修正版训练/验证/测试集）
  - 冻结评测 19 家
  - cn_reports（150 题人工 Gold v1 的来源）
"""
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path('.')

def codes_from_training(d: pathlib.Path) -> set[str]:
    out = set()
    for name in ('train.jsonl', 'dev.jsonl', 'test.jsonl'):
        p = d / name
        if p.is_file():
            for l in p.read_text(encoding='utf-8').splitlines():
                if l.strip():
                    o = json.loads(l)
                    if o.get('company'):
                        out.add(str(o['company']))
    return out

def codes_from_jsonl(p: pathlib.Path, key='company') -> set[str]:
    if not p.is_file():
        return set()
    out = set()
    for l in p.read_text(encoding='utf-8').splitlines():
        if l.strip():
            o = json.loads(l)
            if o.get(key):
                out.add(str(o[key]))
    return out

FROZEN = {'000615','000930','002097','002388','002413','002425','002569','002598','002808',
          '002825','300149','300632','600080','600165','600187','603839','688152','836263','873576'}

mine = codes_from_jsonl(ROOT / '答辩评测/annual_reports_final/training_candidates.jsonl')
repaired = codes_from_training(ROOT / '答辩评测/annual_reports_weak_training_repaired_20261004')
gold_v1 = codes_from_jsonl(ROOT / '答辩评测/human_gold_eval_20261004/human_gold_20261004.jsonl')
# 我的全部语料公司
corpus = set()
mf = ROOT / '答辩评测/annual_reports_final/manifest.jsonl'
if mf.is_file():
    for l in mf.read_text(encoding='utf-8').splitlines():
        if l.strip():
            corpus.add(json.loads(l)['stock_code'])

print('=== 各集合规模 ===')
print(f'  我的语料公司            : {len(corpus)}')
print(f'  其中产出候选的公司      : {len(mine)}')
print(f'  年报修正版训练/验证/测试 : {len(repaired)}')
print(f'  人工 Gold v1（150 题）  : {len(gold_v1)}')
print(f'  冻结评测                : {len(FROZEN)}')
print()
print('=== 重叠核查 ===')
print(f'  修正版训练 ∩ 我的语料   : {len(repaired & corpus)}')
print(f'  修正版训练 ∩ 冻结       : {len(repaired & FROZEN)}')
print(f'  v1 Gold ∩ 我的语料      : {len(gold_v1 & corpus)}')
print(f'  v1 Gold ∩ 冻结          : {len(gold_v1 & FROZEN)}')
print()

excluded = corpus | repaired | FROZEN | gold_v1
print(f'  需排除的公司合计        : {len(excluded)}')

# 我的语料里还剩多少可用于 v3（若只用自己的语料，则已全部被排除）
avail = corpus - excluded
print(f'  我的语料中未被排除的    : {len(avail)}')
print()

print('=== 结论 ===')
if not avail:
    print('  ✗ 我的 491 份语料公司**全部**与训练集重叠，不能直接用于 v3。')
    print('    需要新采一批公司作为 v3 的来源（与训练集公司隔离）。')
else:
    print(f'  ✓ 有 {len(avail)} 家可用于 v3：{sorted(avail)[:20]}')
print()
print('=== 采集需求估算 ===')
print('  v3 目标 240–360 题，每题需 1 个真实年报句 + 同表 3–12 条候选事实。')
print('  按实测产出率（1.79 growth + 0.68 quote 每条语料 / 份）：')
print('    需 240–360 题 → 约 100–150 份新报告（若每题 1 句）')
print('    若要每题多句、多分层，建议采 150–200 份。')
print()
print('=== v3 公司隔离规则（写入构造器）===')
print(f'  EXCLUDE = 我的语料 {len(corpus)} ∪ 修正版训练 {len(repaired)} ∪ 冻结 {len(FROZEN)} ∪ v1 Gold {len(gold_v1)}')
print(f'  共 {len(excluded)} 家')
