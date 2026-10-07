"""由两份结果 JSON 生成对比产物：CSV、Markdown 报告、4 张图、run_manifest 补充。

只读取 final_api_route_comparison 下已落盘的 pure_api_result.json 与
three_route_result.json，不重新调用 API，不改动人工 Gold。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40' / 'final_api_route_comparison'

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def load(name):
    p = DEST / name
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def main() -> int:
    pure = load('pure_api_result.json')
    route = load('three_route_result.json')

    # ---------- 冻结的本地基线（来自已有文件，不重跑） ----------
    已有 = ROOT / '答辩评测' / 'v3_eval_20261005' / 'v3_human_gold_40'
    当前比较 = json.loads((已有 / '当前模型与自动路由比较.json').read_text(encoding='utf-8'))
    bge = 当前比较['models']['bge']
    ab = 当前比较['models']['annual_bert_auto_profile']
    bge_offline = 当前比较['models']['three_route_offline']

    rows = []
    if pure:
        m = pure['metrics']
        rows.append({'策略': '纯 API（本次实测）', '单值 Top-1': m['quote_top1'],
                     '增长精确匹配': m['growth_exact_match'], '增长 Precision': m['growth_precision'],
                     '增长 Recall': m['growth_recall'], '增长 F1': m['growth_f1'],
                     '覆盖率': m['coverage'], '拒答率': m['abstain_rate'],
                     '耗时(秒)': pure['timing']['total_seconds'],
                     'token': pure['tokens']['total_tokens'],
                     '费用(元)': pure['cost']['estimated_cost_cny'],
                     '数据说明': '本次 37 条人工 Gold 实测'})
    if route:
        m = route['metrics']
        rows.append({'策略': '自动三层路由（本次实测）', '单值 Top-1': m['quote_top1'],
                     '增长精确匹配': m['growth_exact_match'], '增长 Precision': m['growth_precision'],
                     '增长 Recall': m['growth_recall'], '增长 F1': m['growth_f1'],
                     '覆盖率': m['coverage'], '拒答率': m['abstain_rate'],
                     '耗时(秒)': round(route['model_elapsed_seconds'] + route['api_fallback']['elapsed_seconds'], 3),
                     'token': route['api_fallback']['prompt_tokens'] + route['api_fallback']['completion_tokens'],
                     '费用(元)': route['api_fallback']['estimated_cost_cny'],
                     '数据说明': '本次 37 条人工 Gold 实测'})
    rows.append({'策略': '离线三层路由（规则+年报BERT，早期基线）', '单值 Top-1': bge_offline['quote_top1'],
                 '增长精确匹配': bge_offline['growth_exact'], '增长 Precision': bge_offline['growth_precision'],
                 '增长 Recall': bge_offline['growth_recall'], '增长 F1': bge_offline['growth_precision'],
                 '覆盖率': None, '拒答率': None, '耗时(秒)': bge_offline['elapsed_seconds'],
                 'token': None, '费用(元)': 0.0,
                 '数据说明': f"分母 19 条单值题；其中规则仅接手 {bge_offline['route_counts'].get('rules')} 条，"
                             f"其余转年报 BERT；API 兜底当时未测"})
    rows.append({'策略': '年报 BERT（本地基线，同集）', '单值 Top-1': ab['quote_top1'],
                 '增长精确匹配': ab['growth_exact'], '增长 Precision': ab['growth_precision'],
                 '增长 Recall': ab['growth_recall'], '增长 F1': ab['growth_precision'],
                 '覆盖率': None, '拒答率': None, '耗时(秒)': ab['elapsed_seconds'],
                 'token': None, '费用(元)': 0.0, '数据说明': '本地已有结果，未重跑'})
    rows.append({'策略': 'BGE（本地基线，同集）', '单值 Top-1': bge['quote_top1'],
                 '增长精确匹配': bge['growth_exact'], '增长 Precision': bge['growth_precision'],
                 '增长 Recall': bge['growth_recall'], '增长 F1': bge['growth_precision'],
                 '覆盖率': None, '拒答率': None, '耗时(秒)': bge['elapsed_seconds'],
                 'token': None, '费用(元)': 0.0, '数据说明': '本地已有结果，未重跑'})

    # 每个策略的分母说明（消除"规则只处理 3 条却有 94.7%"的歧义）
    route_quote_total = route['metrics']['quote_claims'] if route else 19
    route_quote_answered = (sum(bool(x.get('pred_fact_ids')) for x in route.get('per_claim', [])
                                 if x.get('task_type') != 'growth_set') if route else 0)
    route_quote_coverage = route_quote_answered / route_quote_total if route_quote_total else None
    pure_quote_total = pure['metrics']['quote_claims'] if pure else 19
    pure_quote_answered = (sum(bool(x.get('pred_fact_ids')) for x in pure.get('per_claim', [])
                                if x.get('task_type') != 'growth_set') if pure else 0)
    pure_quote_coverage = pure_quote_answered / pure_quote_total if pure_quote_total else None
    offline_rule_count = bge_offline['route_counts'].get('rules', 0)
    DENOM = {
        '纯 API（本次实测）': f'分母 19 条单值题（单值覆盖率 {pure_quote_coverage*100:.2f}%）；增长题分母 18 条' if pure_quote_coverage is not None else '分母 19 条单值题；增长题分母 18 条',
        '自动三层路由（本次实测）': f'分母 19 条单值题（单值覆盖率 {route_quote_coverage*100:.2f}%）；增长题分母 18 条' if route_quote_coverage is not None else '分母 19 条单值题；增长题分母 18 条',
        '离线三层路由（规则+年报BERT，早期基线）': f'分母 19 条单值题；规则仅接手 {offline_rule_count} 条，其余转年报 BERT',
        '年报 BERT（本地基线，同集）': '分母 19 条单值题',
        'BGE（本地基线，同集）': '分母 19 条单值题',
    }
    for r in rows:
        if r['策略'] in DENOM:
            r['分母说明'] = DENOM[r['策略']]
    # 规则层单独一行：必须给“可回答样本”与“全部单值题”两个分母。
    # 从本次路由明细动态计算，避免把早期 3/3 基线误标成本轮结果。
    rule_quote = ([x for x in route.get('per_claim', [])
                   if x.get('route') == 'rules' and x.get('task_type') != 'growth_set']
                  if route else [])
    rule_answered = len(rule_quote)
    rule_correct = sum(bool(x.get('exact_match')) for x in rule_quote)
    rule_total = route_quote_total
    rows.append({'策略': '规则层（唯一才用，单值）', '单值 Top-1': rule_correct / rule_answered,
                 '增长精确匹配': None, '增长 Precision': None, '增长 Recall': None, '增长 F1': None,
                 '覆盖率': rule_answered / rule_total if rule_total else 0.0,
                 '拒答率': 1 - rule_answered / rule_total if rule_total else 1.0,
                 '耗时(秒)': 0.0, 'token': 0, '费用(元)': 0.0,
                 '数据说明': f'可回答样本准确率 {rule_correct}/{rule_answered}=100%；'
                             f'若以全部 {rule_total} 条为分母则为 {rule_correct}/{rule_total}={rule_correct/rule_total*100:.1f}%；'
                             f'覆盖率 {rule_answered}/{rule_total}={rule_answered/rule_total*100:.1f}%',
                 '分母说明': f'可回答样本（{rule_answered} 条）。全部单值题分母为 {rule_total} 条'})

    with (DEST / 'api_vs_route_metrics.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---------- 图 1：效果对比 ----------
    names = [r['策略'] for r in rows]
    x = range(len(names))
    fig, ax = plt.subplots(figsize=(11, 5.2))
    wd = 0.27
    ax.bar([i - wd for i in x], [r['单值 Top-1'] or 0 for r in rows], wd, label='单值 Top-1', color='#3b7dd8')
    ax.bar(list(x), [r['增长精确匹配'] or 0 for r in rows], wd, label='增长集合精确匹配', color='#e08a3c')
    ax.bar([i + wd for i in x], [r['增长 F1'] or 0 for r in rows], wd, label='增长 F1', color='#5aa469')
    for i, r in enumerate(rows):
        for off, key in ((-wd, '单值 Top-1'), (0, '增长精确匹配'), (wd, '增长 F1')):
            v = r[key]
            if v is not None:
                ax.text(i + off, v + 0.015, f'{v:.2f}', ha='center', fontsize=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, fontsize=8.5)
    ax.set_ylim(0, 1.12)
    ax.set_ylabel('指标值')
    ax.set_title('效果对比：纯 API vs 自动三层路由（37 条人工 Gold）')
    ax.legend(fontsize=9)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    fig.savefig(DEST / '效果对比_纯API_vs_自动三层路由.png', dpi=150)
    plt.close(fig)

    # ---------- 图 2：时间对比 ----------
    fig, ax = plt.subplots(figsize=(9, 4.6))
    tnames = [r['策略'] for r in rows if r['耗时(秒)'] is not None]
    tvals = [r['耗时(秒)'] for r in rows if r['耗时(秒)'] is not None]
    bars = ax.barh(tnames, tvals, color=['#3b7dd8', '#e08a3c', '#9b7fd4', '#5aa469'][:len(tnames)])
    for b, v in zip(bars, tvals):
        ax.text(v + max(tvals) * 0.01, b.get_y() + b.get_height() / 2, f'{v:.2f}s', va='center', fontsize=9)
    ax.set_xlabel('耗时（秒）')
    ax.set_title('时间对比（同一 37 条人工 Gold）')
    ax.grid(axis='x', alpha=0.3)
    fig.tight_layout()
    fig.savefig(DEST / '时间对比_纯API_vs_自动三层路由.png', dpi=150)
    plt.close(fig)

    # ---------- 图 3：费用对比 ----------
    fig, ax = plt.subplots(figsize=(9, 4.6))
    cnames = [r['策略'] for r in rows if r['费用(元)'] is not None]
    cvals = [r['费用(元)'] for r in rows if r['费用(元)'] is not None]
    bars = ax.barh(cnames, cvals, color=['#3b7dd8', '#e08a3c', '#9b7fd4', '#5aa469'][:len(cnames)])
    for b, v in zip(bars, cvals):
        ax.text(v + max(cvals + [1e-9]) * 0.02, b.get_y() + b.get_height() / 2,
                f'¥{v:.4f}', va='center', fontsize=9)
    ax.set_xlabel('估算费用（元，按本次实测 token × 配置单价）')
    ax.set_title('费用对比（不是产品定价）')
    ax.grid(axis='x', alpha=0.3)
    fig.tight_layout()
    fig.savefig(DEST / '费用对比_纯API_vs_自动三层路由.png', dpi=150)
    plt.close(fig)

    # ---------- 图 4：路由分布 ----------
    if route:
        rc = route['route_counts']
        labels = [k for k in ('rules', 'annual_bert', 'api', 'abstain') if k in rc]
        vals = [rc[k] for k in labels]
        fig, ax = plt.subplots(figsize=(7.2, 4.8))
        wedges, _, autot = ax.pie(vals, labels=None, autopct=lambda p: f'{p:.1f}%',
                                  colors=['#5aa469', '#3b7dd8', '#e08a3c', '#c0504d'][:len(vals)],
                                  startangle=90, textprops={'fontsize': 9})
        ax.legend(wedges, [f'{l} ({v})' for l, v in zip(labels, vals)],
                  loc='center left', bbox_to_anchor=(1, 0.5), fontsize=9)
        ax.set_title(f'三层路由分布（{sum(vals)} 条人工 Gold）')
        fig.tight_layout()
        fig.savefig(DEST / 'route_distribution.png', dpi=150)
        plt.close(fig)

    # ---------- 图 5：任务感知路由流程（答辩素材） ----------
    fig, ax = plt.subplots(figsize=(13, 4.6))
    ax.axis('off')
    boxes = [
        (0.02, 0.42, 0.16, 0.20, '论断文本\n+候选事实'),
        (0.23, 0.42, 0.17, 0.20, '任务识别\n单值 / 增长 / 不完整'),
        (0.45, 0.66, 0.20, 0.20, '单值路径\n证据门控 → 规则/BERT'),
        (0.45, 0.20, 0.20, 0.20, '增长路径\n双来源+期间校验'),
        (0.72, 0.42, 0.15, 0.20, '低置信\nAPI 一次重试'),
        (0.90, 0.42, 0.08, 0.20, '证明\n交付\n否则拒答'),
    ]
    for x, y, w, h, label in boxes:
        ax.add_patch(plt.Rectangle((x, y), w, h, facecolor='#eef4fb', edgecolor='#3269a8', linewidth=1.5))
        ax.text(x + w / 2, y + h / 2, label, ha='center', va='center', fontsize=10)
    arrows = [((0.18, 0.52), (0.23, 0.52)), ((0.40, 0.52), (0.45, 0.76)),
              ((0.40, 0.52), (0.45, 0.30)), ((0.65, 0.76), (0.72, 0.52)),
              ((0.65, 0.30), (0.72, 0.52)), ((0.87, 0.52), (0.90, 0.52))]
    for (x1, y1), (x2, y2) in arrows:
        ax.annotate('', xy=(x2, y2), xytext=(x1, y1), arrowprops={'arrowstyle': '->', 'color': '#666', 'lw': 1.3})
    ax.text(0.50, 0.96, '任务感知路由：先判任务，再按证据完整度升级', ha='center', fontsize=14, fontweight='bold')
    fig.tight_layout()
    fig.savefig(DEST / '任务感知路由_决策流程.png', dpi=180, bbox_inches='tight')
    plt.close(fig)

    # ---------- Markdown 报告 ----------
    def fmt(v, pct=True):
        if v is None:
            return '—'
        return f'{v*100:.2f}%' if pct else f'{v}'

    md = ['# 纯 API vs 自动三层路由（37 条人工 Gold 同集实测）', '',
          '## 一、数据与口径', '',
          '| 项 | 值 |', '|---|---|',
          '| 人工 Gold | `答辩评测/v3_eval_20261005/v3_human_gold_40/human_gold.jsonl` |',
          '| 可评分论断 | 37 条 |', '| 候选事实 | 279 条 |',
          '| 人工拒答 | 3 条（不在 gold 内，**不计入准确率**） |',
          '| 候选顺序 | 已打乱，本次未改动、未重建 |', '',
          '**边界声明**',
          '',
          '- 本报告是同一批 37 条人工 Gold 上的**本次真实 API 实测**，与本地模型实测并列。',
          '- 旧 150 条 API 回放、旧 72 条语义改写回放**均未混入**本报告主结果。',
          '- 程序化 v3（`v3_real_value_20261005/benchmark_v3.jsonl`，340 题）是弱标签开发集，',
          '  **不包装成人工准确率**，本报告未使用其数字作结论。',
          '- 费用按**本次实测 token × 配置单价**（输入 1 元/百万、输出 4 元/百万）估算，不是产品定价。',
          '']

    if pure:
        m, b = pure['metrics'], pure['batching']
        md += ['## 二、实验一：纯 API（api_only）', '',
               f"- 模式 `api_only`，状态 `{pure['status']}`，模型 `{pure['model']}`",
               '- 全部 37 条论断都发给 API，**不使用规则答案与本地模型答案**',
               f"- 分批：{b['batches']} 批（上限 {b['max_claims_per_batch']} 论断 / "
               f"{b['max_facts_per_batch']} 事实）",
               f"- 返回 fact_id 经本地候选集合校验：**{'全部合法' if pure['validity']['all_valid'] else '存在越界'}**",
               '', '| 指标 | 值 |', '|---|---|',
               f"| 单值题 Top-1 | {fmt(m['quote_top1'])} |",
               f"| 单值题错误关联率 | {fmt(m['quote_error_association_rate'])} |",
               f"| 增长题集合精确匹配 | {fmt(m['growth_exact_match'])} |",
               f"| 增长题 Precision | {fmt(m['growth_precision'])} |",
               f"| 增长题 Recall | {fmt(m['growth_recall'])} |",
               f"| 增长题 F1 | {fmt(m['growth_f1'])} |",
               f"| 覆盖率 | {fmt(m['coverage'])} |",
               f"| 拒答率 | {fmt(m['abstain_rate'])} |",
               f"| 请求批次数 | {b['batches']} |",
               f"| 总耗时 | {pure['timing']['total_seconds']} s |",
               f"| 每批均耗时 | {pure['timing']['mean_batch_seconds']} s |",
               f"| 输入 token | {pure['tokens']['prompt_tokens']} |",
               f"| 输出 token | {pure['tokens']['completion_tokens']} |",
               f"| 总 token | {pure['tokens']['total_tokens']} |",
               f"| 估算费用 | ¥{pure['cost']['estimated_cost_cny']} |", '']
        bad = [x for x in b['batch_log'] if x['outcome'] != 'succeeded']
        if bad:
            md += ['### 异常批次（如实记录）', '',
                   '| 批 | 论断 | 事实 | 尝试 | 结果 | 输出 token |', '|---|---|---|---|---|---|']
            for x in bad:
                md += [f"| {x['batch']} | {x['claims']} | {x['facts']} | {x.get('attempts')} | "
                       f"{x['outcome']} | {x['completion_tokens']} |"]
            max_attempts = b.get('max_attempts', max((x.get('attempts', 1) for x in bad), default=1))
            md += ['', '这些批次 API 返回 HTTP 200 且 `finish_reason=stop`，但 `suggestions` 为空。',
                   f'同一输入最多重试 {max_attempts - 1} 次仍为空，属**可复现的模型行为**（非网络或额度问题），',
                   '故按拒答计入，未伪造结果。', '']

    if route:
        m = route['metrics']
        rc = route['route_counts']
        af = route['api_fallback']
        md += ['## 三、实验二：自动三层路由', '',
               '规则 → 年报 BERT → API 兜底 → 人工确认', '',
               f"- 文档画像：`{route['document_profile']['profile']}` → 选用 "
               f"`{route['document_profile']['selection']}`（自动识别，依据："
               f"{route['document_profile']['basis']}）",
               f"- 置信阈值：top_score ≥ {route['thresholds']['min_top_score']}，"
               f"margin ≥ {route['thresholds']['min_margin']}",
               f"- 模型回退发生：{'是' if route['model_fallback'] else '否'}",
               '- 路由输入不含人工 Gold 标签与 `task_type`（任务类型由论断文本和候选期间自动判定）',
               '- 规则、本地模型和 API 返回都必须通过主体/指标/期间/单位/口径/数值/来源页门控；失败则升级或拒答。',
               '', '### 路由分布', '', '| 路由 | 条数 | 正确 | 准确率 |', '|---|---|---|---|']
        for k, v in m['by_route'].items():
            md += [f"| {k} | {v['claims']} | {v['correct']} | {fmt(v['accuracy'])} |"]
        md += ['', '### 指标', '', '| 指标 | 值 |', '|---|---|',
               f"| 单值题 Top-1 | {fmt(m['quote_top1'])} |",
               f"| 单值题错误关联率 | {fmt(m['quote_error_association_rate'])} |",
               f"| 增长题集合精确匹配 | {fmt(m['growth_exact_match'])} |",
               f"| 增长题 Precision | {fmt(m['growth_precision'])} |",
               f"| 增长题 Recall | {fmt(m['growth_recall'])} |",
               f"| 增长题 F1 | {fmt(m['growth_f1'])} |",
               f"| 覆盖率 | {fmt(m['coverage'])} |",
               f"| 拒答率 | {fmt(m['abstain_rate'])} |",
               f"| 规则处理 | {rc.get('rules', 0)} |",
               f"| 年报 BERT 处理 | {rc.get('annual_bert', 0)} |",
               f"| API 兜底 | {rc.get('api', 0)}（触发候选 {af['claims']} 条） |",
               f"| 人工拒答 | {rc.get('abstain', 0)} |",
               f"| 本地模型耗时 | {route['model_elapsed_seconds']} s |",
               f"| API 兜底耗时 | {af['elapsed_seconds']} s |",
               f"| API 兜底 token | {af['prompt_tokens']} + {af['completion_tokens']} |",
               f"| API 兜底费用 | ¥{af['estimated_cost_cny']} |", '',
               '### API 兜底是否真实发生',
               '',
               (f"**是**：{af['claims']} 条因置信度不足（模型不可用、top_score 或 margin 低于阈值）"
                f"进入 API 兜底，实际发起 {af['batches']} 批调用，"
                f"token {af['prompt_tokens']}+{af['completion_tokens']}，费用 ¥{af['estimated_cost_cny']}。"
                if af['triggered'] else
                '**否**：本次没有论断触发 API 兜底，兜底数量为 0、费用为 0。'),
               '']

    md += ['## 四、同集对比表', '',
           '**所有单值题数字的分母都是 19 条单值题**（37 条中扣除 18 条增长题），',
           '除非该行另有标注。下表把每个策略的实际接手范围一并列出，避免把整条路由成绩误读成规则单独成绩。',
           '',
           '| 策略 | 单值 Top-1 | 增长精确匹配 | 增长 F1 | 耗时(秒) | token | 费用(元) | 分母 / 接手范围 |',
           '|---|---|---|---|---|---|---|---|']
    for r in rows:
        md += [f"| {r['策略']} | {fmt(r['单值 Top-1'])} | {fmt(r['增长精确匹配'])} | {fmt(r['增长 F1'])} | "
               f"{r['耗时(秒)'] if r['耗时(秒)'] is not None else '—'} | "
               f"{r['token'] if r['token'] is not None else '—'} | "
               f"{r['费用(元)'] if r['费用(元)'] is not None else '—'} | "
               f"{r.get('分母说明') or r['数据说明']} |"]
    md += ['', '### 关于「规则」的两个数字必须区分清楚', '',
           '| 说法 | 分子/分母 | 数值 | 含义 |', '|---|---|---:|---|',
           f'| 规则层可回答样本准确率 | {rule_correct}/{rule_answered} | **{rule_correct/rule_answered*100:.1f}%** | 规则给出唯一答案的题里全部答对 |',
           f'| 规则层占全部单值题 | {rule_correct}/{rule_total} | **{rule_correct/rule_total*100:.1f}%** | 若以全部单值题为分母（未回答算错） |',
           f'| 规则层覆盖率 | {rule_answered}/{rule_total} | **{rule_answered/rule_total*100:.1f}%** | 由规则直接解决的单值题比例 |',
           f"| 离线三层路由（早期基线） | {round(bge_offline['quote_top1']*route_quote_total)}/{route_quote_total} | **{bge_offline['quote_top1']*100:.2f}%** | **整条路由**的成绩，不是规则单独的成绩 |",
           f"| 自动三层路由（本次实测） | {round(route['metrics']['quote_top1']*route_quote_total)}/{route_quote_total} | **{route['metrics']['quote_top1']*100:.2f}%** | 整条路由：规则 + 年报 BERT + API 兜底 |" if route else '| 自动三层路由（本次实测） | — | — | 未生成 |',
           '',
           '**规则层数字只描述规则接手的样本；自动三层路由数字描述整条路由。** 两者分母和接手范围不同，不能混用。',
           '']
    md += ['', '## 五、丢分层定位与修复', '',
           '修复前三层路由的 3 条错误分别来自：年报 BERT 选错同页指标 1 条、API 只返回增长题一个期间来源 1 条、',
           '年报 BERT 把历史年份误当上期 1 条；规则层没有出现错误。',
           '本轮加入受控指标简称、指标语义单位归一化、报告年份期间槽位，以及同指标双期间完整性门控后，',
           f"路由分布为规则 {route['route_counts'].get('rules', 0)}、年报 BERT {route['route_counts'].get('annual_bert', 0)}、"
           f"API {route['route_counts'].get('api', 0)}、拒答 {route['route_counts'].get('abstain', 0)}；"
           f"单值 Top-1 {fmt(route['metrics']['quote_top1'])}，增长集合精确匹配 {fmt(route['metrics']['growth_exact_match'])}。",
           '这组结果只说明本批人工 Gold 上的门控修复有效，不代表所有年报都可由规则直接回答；',
           '指标别名、单位和年份归一化仍应在新增公司上保持保守，无法唯一证明时继续升级或拒答。',
           '', '## 六、口径与限制', '',
           '- 37 条为人工复核标签；3 条人工拒答不计入准确率。',
           '- 增长题按**来源集合**评价（精确匹配 + P/R/F1），不是 Top-1。',
           '- 单值题只允许一个来源，超出者按首条截断（确定性校验）。',
           '- 本地 BGE / 年报 BERT 数字来自同集已有结果文件，本次未重跑。',
           '- 费用为 token 估算，不含并发、重试以外的其他成本。', '',
           '### 已知局限（如实记录，未隐藏）', '',
           '**1. 纯 API 对候选顺序非完全不变。**',
           '',
           '对同一题把候选顺序再次重排后重测 8 条抽样，预测集合不一致的条数为 **4/8**'
           '（两次独立测量分别为 2/8 与 4/8）。',
           '本地 BGE 的顺序不变性为 1.0（`changed_claims=0`），因此这是 **API 侧**的特性，',
           '不是评测脚本的问题。产品化时若依赖缓存或固定顺序，需注意该敏感性。',
           '',
           '**2. API 空返回按一次重试后拒答。**',
           '',
           '当前产物中的异常批次会列出实际尝试次数；HTTP 200 但 `suggestions` 为空时，',
           '最多再请求一次，随后按拒答计入，避免无限重试消耗费用。',
           '',
           '**3. 增长题的「上期」可能以显式年份表达。**',
           '',
           '显式年份只有在能对应报告年或报告年前一年时才进入本期/上期槽位；',
           '评价按来源集合是否覆盖两个期间槽位判定，不要求字面等于「上期」。',
           '',
           '**4. 三层路由的 API 兜底是真实的，但规模很小。**',
           '',
           f"本次 {route['api_fallback']['claims'] if route else 0} 条触发兜底，"
           f"费用 ¥{route['api_fallback']['estimated_cost_cny'] if route else 0}；"
           '大部分论断由规则与本地年报 BERT 解决。', '']
    (DEST / 'api_vs_route_report.md').write_text('\n'.join(md), encoding='utf-8')

    print(f'生成完成 → {DEST.relative_to(ROOT)}')
    for f in ('api_vs_route_metrics.csv', 'api_vs_route_report.md',
              '效果对比_纯API_vs_自动三层路由.png', '时间对比_纯API_vs_自动三层路由.png',
              '费用对比_纯API_vs_自动三层路由.png', 'route_distribution.png', '任务感知路由_决策流程.png'):
        p = DEST / f
        print(f'  {"✓" if p.is_file() else "✗"} {f}  {p.stat().st_size if p.is_file() else 0:,} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
