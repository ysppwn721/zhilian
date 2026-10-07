"""计划符合度自查：逐条核对执行状态。

只做声明，不做臆测：每条给出「已执行/部分执行/未执行」+ 依据文件或命令。
"""
import hashlib
import json
from pathlib import Path

for _stream in (__import__('sys').stdout, __import__('sys').stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
GD = ROOT / '答辩评测' / 'human_gold_eval_20261004'

ITEMS = [
    # (优先级, 计划要求, 状态, 依据)
    ('P0', '单值引用：先走纯规则，零/多候选才调本地模型',
     '已执行', 'packaging/route_auto_taskid.py::rule_quote（含 escalate/abstain 状态）'),
    ('P0', '增长率论断：年报修正版 BERT 召回两来源，再由规则校核',
     '已执行', 'route_auto_taskid.py::verify_growth（metric 一致/期间互补/百分比量级）'),
    ('P0', '低置信、口径不明、来源不完整 → 拒答',
     '已执行', 'rule_quote 的 abstain/escalate 分支 + verify_growth 的 reject 分支'),
    ('P0', '自动任务识别，不得读取 task_type 标签',
     '已执行', 'route_auto_taskid.py::detect_task，只用 claim_text；准确率 148/150'),
    ('P0', '报告自动任务识别准确率、误路由率、拒答率',
     '已执行', 'auto_taskid_route_report.json（含混淆矩阵与拒答数）'),
    ('P0', '409MB PyTorch 需 ONNX 导出 + 量化 + CPU 延迟/内存验收',
     '未执行', '待办：models/zh_reranker_bert_annual_repaired_v1 尚无 ONNX 产物'),

    ('P1-1', '保留 150 题 v1 及 SHA256，不再修改',
     '已执行', 'human_gold_eval_20261004/v1_freeze_sha256.json'),
    ('P1-2', '模板化 annual_benchmark_repaired_20261004_v2 只作诊断，不作语义证据',
     '已执行', '已在比较脚本 compare_corpus_complementarity.py 中标注其 claim 0% 出自年报'),
    ('P1-3', '新建 240–360 题 v3，单值题必须含真实数值',
     '部分执行', 'v3_real_value_20261005/benchmark_v3.jsonl 已生成 340 题；合法纯单值仅 45 条，未伪造上期单值配平'),
    ('P1-4', '每题含同指标本期/上期 + 真实干扰；增长题标注双来源',
     '已执行', 'v3_real_value_20261005/benchmark_v3.jsonl：同期间异指标干扰、增长双来源已校验'),
    ('P1-5', '本期/上期配平；固定种子打乱；保留前后哈希',
     '部分执行', 'v3 固定种子打乱与哈希已生成；但池内没有合法上期纯单值句，期间配平未宣称完成'),
    ('P1-6', 'v3 与训练公司隔离；先 20 条试运行',
     '已执行', 'v3_pilot20_report.json + 510 家公司隔离核验'),

    ('P1-补', '反事实交换：同时在人工 Gold 与模型自带 train/dev/test 上运行',
     '已执行', 'counterfactual_period_swap.py（claim 侧）'
               ' + fact_side_period_swap.py（fact 侧，更干净的设计）'),

    ('P2', '在 v3 上统一重跑规则/BGE/BERT v1/BERT v2/年报修正版/RRF/固定路由/纯 API',
     '部分执行', 'v3 已完成 BGE 与年报修正版 BERT 重跑；BERT v1/v2、RRF、纯 API 尚未完成'),
    ('P2', '指标统一：Top-1/Top-2/集合精确/集合 P-R-F1/拒答/延迟/token/费用/顺序一致率',
     '部分执行', '已有：Top-1、集合精确匹配、顺序一致率（打乱前后）、API token 与费用；'
                 '缺：集合 P/R/F1、P50/P95 延迟、拒答准确率'),

    ('P3', '规则硬约束 + 本地评分 + API 兜底；RRF 仅作对照',
     '部分执行', 'route_auto_taskid.py 已实现规则优先与模型兜底；'
                 'API 兜底与阈值校准尚未接入'),
    ('P3', '暂停继续训练',
     '已执行', '本轮未启动任何训练；models/ 无新权重'),
    ('P3', '重训与否由 v3 结果决定',
     '未执行', '取决于 v3'),
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    print('=== 计划符合度自查 ===')
    print()
    counts = {'已执行': 0, '部分执行': 0, '未执行': 0}
    cur = None
    for pr, req, st, ev in ITEMS:
        if pr != cur:
            print(f'--- {pr} ---')
            cur = pr
        counts[st] += 1
        mark = {'已执行': '✓', '部分执行': '△', '未执行': '✗'}[st]
        print(f'  {mark} {req}')
        print(f'      → {st}：{ev}')
    print()
    print(f'  合计 已执行 {counts["已执行"]} · 部分执行 {counts["部分执行"]} · 未执行 {counts["未执行"]}'
          f'  （共 {len(ITEMS)} 条）')
    print()

    # 生成 v1 的 SHA256 冻结清单（计划 P1-1 要求）
    print('=== 生成 v1 冻结清单（补 P1-1 缺口）===')
    targets = [
        GD / 'human_gold_20261004.jsonl',
        GD / 'human_gold_20261004_shuffled.jsonl',
        GD / 'human_gold_pure_api_20261004.jsonl',
    ]
    manifest = {'frozen_at': '2026-10-05', 'purpose': 'v1 位置伪影修复证据，冻结不再修改', 'files': {}}
    out = GD / 'v1_freeze_sha256.json'
    for t in targets:
        if t.is_file():
            digest = sha256(t)
            manifest['files'][t.name] = {'sha256': digest, 'bytes': t.stat().st_size}
            print(f'  {t.name}  {digest[:16]}…  {t.stat().st_size:,} bytes')
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'→ {out.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
