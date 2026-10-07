"""真实中文语料表达方式：年报用词、千分位数值、动词变体。

背景：引擎在项目自建的合成语料上识别率 100%，但在真实上市公司年报上
识别率约 0.5%（580 条含数字句子中仅 3 条）。本文件锁定已补齐的那部分能力，
并明确记录仍未覆盖的部分，避免再次出现「合成语料 100% = 真实泛化能力」的误判。
"""
import pytest

from zhilian.engine import best_facts, check, extract_claims, number


def kinds(text, facts=()):
    return [c['kind'] for c in extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't', 'text': text}, list(facts))]


# ---- 千分位数值 ----------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('3,379.37', '3379.37'),
    ('1,234,567.89', '1234567.89'),
    ('-1,000', '-1000'),
    ('125', '125'),
    ('0.5', '0.5'),
])
def test_thousand_separator_is_parsed(raw, expected):
    assert str(number(raw)) == expected


@pytest.mark.parametrize('bad', ['1,23', ',123', '1,', '12,3456'])
def test_malformed_separator_is_rejected(bad):
    """逗号不按三位分组时不剥离，避免把异常输入悄悄改成一个别的数。"""
    with pytest.raises(ValueError):
        number(bad)


def test_quote_with_thousand_separator_is_extracted():
    assert kinds('报告期内投资收益为3,379.37万元') == ['quote']
    claim = extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't', 'text': '投资收益为3,379.37万元'}, [])[0]
    assert claim['spec']['reported'] == pytest.approx(3379.37)


@pytest.mark.parametrize('text,value,status', [
    ('本期经营活动产生的现金流量净额为13.67亿元', '1367348095.07', 'consistent'),
    ('本期经营活动产生的现金流量净额为13.68亿元', '1367348095.07', 'inconsistent'),
    ('本期经营活动产生的现金流量净额为13.67亿元', '1367500000', 'inconsistent'),
    ('本期经营活动产生的现金流量净额为1,367,348,095.07元', '1367348095.08', 'inconsistent'),
    ('本期经营活动产生的现金流量净额为-13.67亿元', '-1367348095.07', 'consistent'),
])
def test_original_display_precision_and_currency_conversion(text, value, status):
    facts = [{'id': 'f', 'subject': '总计', 'metric': '经营活动产生的现金流量净额',
              'period': '本期', 'unit': '元', 'value': value, 'scope': '合并'}]
    c = extract_claims({'file_id': 'F', 'location': ['p', 0], 'label': '正文', 'text': text}, facts)[0]
    assert check(c, facts)['status'] == status


@pytest.mark.parametrize('text', [
    '公司本期实现营收125万元',
    '营业收入125万元',
    '本期销售额录得125万元',
    '本期销售额共计125万元',
    '本期销售额增至125万元',
    '本期销售额高达125万元',
    '本期销售额约为125万元',
    '本期销售额累计125万元',
])
def test_quote_vocabulary_variants_are_recognized(text):
    """年报常见连接词和标题式金额应进入论断层。"""
    assert kinds(text) == ['quote']


# ---- 增长率用词 ----------------------------------------------------------

@pytest.mark.parametrize('text', [
    '较上期增长25%',          # 原有表达，必须保持可用
    '较上期下降10%',
    '营业收入同比增长25%',     # 年报常用「同比」
    '营业收入较上年增加553.21%',  # 年报用「较上年」+「增加」
    '营业成本较上年减少12.5%',
    '营业收入比上年上升8%',
    '公司产品长链二元酸产量增加179.79%',
])
def test_growth_vocabulary_variants_are_recognized(text):
    assert kinds(text) == ['growth']


@pytest.mark.parametrize('text,direction', [
    ('营业收入同比增长25%', 1),
    ('营业收入较上年增加553.21%', 1),
    ('营业收入比上年上升8%', 1),
    ('营业成本较上年减少12.5%', -1),
    ('较上期下降10%', -1),
    ('较上期持平', 0),
])
def test_growth_direction_is_mapped_from_vocabulary(text, direction):
    """方向词可能是否定含义的动词，必须映射正确，否则会把下降读成增长。"""
    spec = extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't', 'text': text}, [])[0]['spec']
    assert spec['direction'] == direction


def test_negative_direction_reports_negative_value():
    spec = extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't',
         'text': '营业成本较上年减少12.5%'}, [])[0]['spec']
    assert spec['reported'] < 0


def test_qualitative_growth_still_supported():
    spec = extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't',
         'text': '营业收入同比增长'}, [])[0]['spec']
    assert spec['qualitative'] is True


@pytest.mark.parametrize('text', ['营业收入同比减少573.71万元', '营业收入同比增长率25%',
                                 '营业收入增加一倍', '提高了服务质量'])
def test_amount_changes_are_not_reported_as_qualitative_percentage_growth(text):
    assert 'growth' not in kinds(text)


def test_bare_percentage_requires_user_to_complete_comparison_period():
    facts = [{'id': p, 'subject': '总计', 'metric': '营业收入', 'period': p,
              'value': 100, 'unit': '万元', 'scope': '合并'} for p in ('本期', '上期')]
    claim = extract_claims({'file_id': 'F', 'location': ['p', 0], 'label': 't',
                            'text': '营业收入增加25%'}, facts)[0]
    assert claim['kind'] == 'growth' and claim['spec']['reported'] == 25
    assert claim['refs'] == [] and claim['confirmed'] is False
    assert claim['spec']['comparison_period_explicit'] is False
    assert '比较基准未明确' in claim['issue']


# ---- 阈值用词 ------------------------------------------------------------

@pytest.mark.parametrize('text', [
    '支出未超过预算',
    '本期销售额超过120万元',
    '营业收入占比超过50%',
    '支出不少于100万元',
    '本期支出低于100万元',
])
def test_threshold_variants_are_recognized(text):
    assert kinds(text) == ['threshold']


def test_threshold_limit_with_separator():
    spec = extract_claims(
        {'file_id': 'F', 'location': ['p', 0], 'label': 't',
         'text': '本期销售额超过1,200万元'}, [])[0]['spec']
    assert spec['limit'] == pytest.approx(1200.0)


@pytest.mark.parametrize('metric,phrase', [('营业收入','营收'), ('资产总计','总资产'), ('负债总计','总负债'), ('员工人数','员工总数')])
def test_metric_aliases_recall_canonical_fact(metric, phrase):
    facts = [{'id': 'f1', 'subject': '公司', 'metric': metric, 'period': '本期',
              'value': 125, 'unit': '万元', 'scope': '合计'}]
    assert [f['id'] for f in best_facts(f'公司本期实现{phrase}125万元', facts)] == ['f1']


@pytest.mark.parametrize('metric,phrase', [('营业收入','主营业务收入'), ('研发费用','研发投入'), ('货币资金','现金及现金等价物')])
def test_accounting_scopes_are_not_aliases(metric, phrase):
    facts = [{'id':'f1', 'subject':'公司', 'metric':metric, 'period':'本期'}]
    assert best_facts(f'公司本期{phrase}', facts) == []


# ---- 已知缺口：明确记录为「尚未支持」------------------------------------

@pytest.mark.parametrize('text,reason', [
    ('营业收入占比已超过公司全部营业收入的50%以上', '算子与数值之间隔着较长定语'),
    ('本期毛利率低于去年同期', '与历史期间比较但无数值'),
    ('生物基、淀粉基新材增加187.44', '表格残片，无单位'),
])
def test_known_gaps_are_documented_not_silently_wrong(text, reason):
    """这些是真实年报里的表达，当前引擎识别不到。

    这里断言「识别不到」而非「识别对了」，是为了把缺口固定在测试里：
    一旦后续补齐，本测试会失败，从而提醒同步更新文档与评测口径。
    """
    assert kinds(text) == [], f'缺口已变化（{reason}），请更新文档与评测口径'


def test_uncovered_metrics_do_not_inherit_cashflow_source():
    facts = [{'id': 'cash', 'subject': '总计', 'metric': '经营活动产生的现金流量净额',
              'period': '本期', 'value': 1367348095.07, 'unit': '元', 'scope': '合并'}]
    text = ('报告期内公司经营活动产生的现金流量净额为1,367,348,095.07元，'
            '本年度净利润为639,663,726.67元，差异727,684,368.40元。')
    claims = extract_claims({'file_id': 'F', 'location': ['p', 0], 'label': '正文', 'text': text}, facts)
    assert [c['refs'] for c in claims] == [['cash'], [], []]


def test_new_metric_does_not_inherit_growth_source():
    facts = [{'id': p, 'subject': '总计', 'metric': '营业收入', 'period': p,
              'value': v, 'unit': '万元', 'scope': '合并'} for p, v in [('本期', 125), ('上期', 100)]]
    for text in ['本期营业收入为125万元，净利润同比增长25%。',
                 '本期营业收入为125万元。净利润同比增长25%。']:
        claims = extract_claims({'file_id': 'F', 'location': ['p', 0], 'label': '正文', 'text': text}, facts)
        assert next(c for c in claims if c['kind'] == 'growth')['refs'] == []
