"""Deterministic, typed claim checks. No eval, generated code or model arithmetic."""
from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

KINDS = {'quote': '数值引用', 'growth': '增长率', 'ranking': '排名', 'threshold': '阈值', 'chart': '图表'}
EXTRACTION_VERSION = 5
UNITS = {'元': ('currency', Decimal(1)), '万元': ('currency', Decimal(10000)),
         '亿元': ('currency', Decimal(100000000)), '件': ('count', Decimal(1)),
         '人': ('people', Decimal(1)), '%': ('percentage', Decimal(1))}
# 允许千分位逗号：真实年报普遍写作 3,379.37 万元，旧规则只认连续数字。
NUM = r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?'
UNIT = r'亿元|万元|元|件|人|%'


def number(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError('布尔值不能作为数值')
    text = str(value).strip()
    # 去千分位。仅当逗号确实按三位分组时才剥离，避免误改 "1,23" 这类异常输入。
    if re.fullmatch(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?', text):
        text = text.replace(',', '')
    try:
        n = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError('请输入有效数字')
    if not n.is_finite() or abs(n) > Decimal('1e15'):
        raise ValueError('数字必须有限且绝对值不超过 10^15')
    return n


def fmt(value) -> str:
    n = number(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return format(n, 'f').rstrip('0').rstrip('.') if '.' in format(n, 'f') else str(n)


def convert(value, unit, target):
    if unit == target:
        return number(value)
    a, b = UNITS.get(unit), UNITS.get(target)
    if not a or not b or a[0] != b[0]:
        raise ValueError(f'单位不兼容：{unit} 与 {target}')
    return number(value) * a[1] / b[1]


class CheckError(ValueError):
    """带分类码的核验失败。

    旧实现靠比对错误文案字符串来判断分类，任何一处提示文案被润色
    （例如"请"改成"您"）都会让分类静默退化为 needs_review，进而影响
    前端诊断分组与报告分组。分类改走结构化字段后不再与文案耦合。
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def stable_id(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:18]


# 主体为这些值时不参与定位：它们描述的是"整体"，任何句子都可能命中。
GENERIC_SUBJECTS = ('总计', '整体', '全部')


# 期间是封闭词表，同义写法属于确定性规则该覆盖的范围：真实文档写"报告期/本年/
# 去年同期"，而事实表只写"本期/上期"。不归一化会让上期与本期同分并列，一条普通
# 数值引用被判成"口径不唯一"，甚至让阈值/增长率拿不到唯一来源。
PERIOD_SYNONYMS = {
    '本期': ('本期', '本报告期', '报告期内', '报告期', '本年度', '本年', '当期', '今年'),
    '上期': ('上期', '上年度同期', '上年同期', '去年同期', '上年度', '上年', '去年'),
}

# 只扩充定义相同的名称。营业总收入、主营收入、现金等价物、研发投入
# 均有独立会计口径，不能因词面相似合并；这些仍交给语义候选与用户确认。
METRIC_ALIASES_BY_CANONICAL = {
    '营业收入': ('营业收入', '营收'),
    '资产总计': ('资产总计', '总资产'),
    '总资产': ('资产总计', '总资产'),
    '负债总计': ('负债总计', '总负债'),
    '总负债': ('负债总计', '总负债'),
    '员工人数': ('员工人数', '员工总数'),
    '员工总数': ('员工人数', '员工总数'),
}


def metric_aliases(metric):
    """Return the closed lexical alias set for one fact metric."""
    metric = str(metric or '')
    if metric in METRIC_ALIASES_BY_CANONICAL:
        return METRIC_ALIASES_BY_CANONICAL[metric]
    return (metric,) if metric else ()


def period_in_text(period, text):
    """事实的期间是否在文本中以任意同义写法出现。"""
    return any(word in text for word in PERIOD_SYNONYMS.get(period, (period,)) if word)


def _descriptors(fact):
    """一条事实的**定位**描述符：指标与非泛指主体。

    期间刻意不放进这里。旧实现中期间只参与加分，不参与"能否匹配"；
    把它列为必需项会让「A产品销量最高」这类没写期间的句子匹配不到任何事实。
    需要按期间收窄时由 best_facts(period=...) 显式过滤。
    """
    keys = []
    if fact.get('metric'):
        keys.append(fact['metric'])
    if fact.get('subject') and fact['subject'] not in GENERIC_SUBJECTS:
        keys.append(fact['subject'])
    return keys


def build_index(facts):
    """描述符 → 事实ID 的反向索引。

    原实现每段文本都遍历全部事实做子串包含判断，一段的开销是
    O(事实数 × 描述符数)。索引把"哪些事实可能相关"提前算好，
    长文档导入时每段只需检查命中描述符所涉及的事实。
    """
    token_ids = {}
    for fact in facts:
        for key in _descriptors(fact):
            token_ids.setdefault(key, set()).add(fact['id'])
        for alias in metric_aliases(fact.get('metric')):
            token_ids.setdefault(alias, set()).add(fact['id'])
    return {'token_ids': token_ids}


_INDEX_CACHE = {}
_INDEX_CACHE_MAX = 8


def facts_index(facts, bypass_cache=False):
    """按描述符构造（并可复用）反向索引。改数值不影响索引，描述符变化时自动重建。

    键是指纹，因此命中条件是"这张事实表的指纹已在缓存里"。旧实现写成
    `entry['size'] != len(facts)`——指纹相同则事实条数必然相同，这个比较永远为
    假，真正的失效场景（同条数不同描述符）反而漏网；同时 `> 8` 的整表清空让
    连续处理 9 张不同事实表后缓存归零，跨请求基本零命中。
    """
    if bypass_cache:
        return build_index(facts)
    fingerprint = hash(tuple(sorted(
        (f['id'], f.get('metric', ''), f.get('subject', ''), f.get('period', '')) for f in facts)))
    entry = _INDEX_CACHE.get(fingerprint)
    if entry is None:
        entry = build_index(facts)
        if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[fingerprint] = entry
    return entry


def best_facts(text, facts, *, period=None, index=None):
    """找出文本对应的唯一事实来源。

    语义与旧实现一致：指标、非泛指主体都必须出现在文本中（至少含指标），
    期间出现时额外加分；period 是额外的显式过滤条件。多条并列时返回全部
    候选，由上层判为"口径不唯一"并转人工确认。
    """
    if not text:
        return []
    index = index or build_index(facts)
    token_ids = index['token_ids']
    hit_tokens = [token for token in token_ids if token in text]
    if hit_tokens:
        wanted = set()
        for token in hit_tokens:
            wanted |= token_ids[token]
        candidates = [f for f in facts if f['id'] in wanted]
    else:
        candidates = facts

    scored = []
    for fact in candidates:
        keys = _descriptors(fact)
        if not keys or not any(alias in text for alias in metric_aliases(fact.get('metric'))):
            continue
        if fact.get('subject') and fact['subject'] not in GENERIC_SUBJECTS and fact['subject'] not in text:
            continue
        if period and fact['period'] != period:
            continue
        score = 5
        if fact['subject'] not in GENERIC_SUBJECTS:
            score += 4
        if fact['period'] and period_in_text(fact['period'], text):
            score += 3
        scored.append((score, fact))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [f for s, f in scored if s == top]


def ranking_cohort(metric, facts):
    """排名称谓的可比集合：同指标、同期间的同一口径组。

    排名只有在比较集合完整时才有意义。集合不足两条时必须把"待补齐"暴露给
    用户，而不是把单条事实当成"最高"的依据；因此抽取和人工候选共用这一处
    逻辑，避免两边各写一份判断。
    """
    if not metric:
        return []
    return [f['id'] for f in facts if f['metric'] == metric and f['period'] == '本期']


def growth_pair(metric, facts):
    """增长率所需的同口径上期/本期配对；只有两侧各唯一时才成立。

    与 ranking_cohort 同理：增长率论断的证据是一对事实，不是一条。当文本只提供
    了其中一侧的线索时，用它的指标在事实表里补另一侧，避免把"无法确定"直接推成
    无候选的死结——人工需要看到一个可勾选的配对。
    """
    if not metric:
        return []
    prev = [f['id'] for f in facts if f['metric'] == metric and f['period'] == '上期']
    curr = [f['id'] for f in facts if f['metric'] == metric and f['period'] == '本期']
    return [prev[0], curr[0]] if len(prev) == 1 and len(curr) == 1 else []


def growth_pairs(facts):
    """事实表里所有"上期+本期"齐全的指标配对，按指标去重后返回。

    增长率句子常常只写「较上期增长25%」，上下文里也没有指标名，此时规则无法确定
    它指哪个指标。与其给出零候选（界面上无任何可勾选项、decide 直接报错），不如把
    事实表里所有成对的指标作为候选交给人工，每条都带确定性核验结论。
    """
    pairs = []
    for metric in dict.fromkeys(f.get('metric') for f in facts):
        pair = growth_pair(metric, facts)
        if pair:
            pairs.append((metric, pair))
    return pairs


def extract_claims(block, facts, index=None):
    """Extract separate, non-overlapping assertions, retaining their exact text anchors."""
    result = []
    context = ''
    # 每段只构造/复用一次索引，避免对每条事实重复扫描文本。
    index = index or facts_index(facts)
    patterns = [
        # 真实年报用「较上年/同比/比上年」，且动词常用「增加/减少」而非「增长/下降」。
        # 旧规则只认「较上期|环比」+「增长|下降|持平」，导致年报语料识别率为 0。
        # 年报有两种稳定写法：带比较期间（同比增长25%），以及在同一
        # 句子中直接写“产量增加179.79%”。后者只有在动词后紧跟百分比
        # 时才启用，避免把普通叙述中的“增加”误识别成增长率论断。
        ('growth', r'(?:较上期|环比|较上年|比上年|同比)(增长|下降|持平|增加|减少|上升)(?:(' + NUM + r')%)?'),
        ('ranking', r'(.+?)(销售额|销量|收入|支出|得分)(?:并列)?最高'),
        ('threshold', r'(未超过|不超过|超过|不少于|低于|高于)\s*(' + NUM + r')\s*(' + UNIT + r')'),
        ('budget', r'支出(未超过|不超过|超过)预算'),
        # 年报中的数值引用连接词远多于“为/是/达到”。连接词允许和数字之间
        # 有少量空白；末尾的“无连接词”分支覆盖“营业收入125万元”这类
        # 标题式写法。阈值模式排在前面，因此“超过120万元”仍优先识别为阈值。
        ('quote', r'(?:(?:约为|实现了|录得了|增至|高达|实现|录得|共计|合计|完成|累计|近|为|是|达到|达)\s*|'
                  r'(?=(?:' + NUM + r')\s*(?:亿元|万元|元|件|人)(?!\w)))('
                  + NUM + r')\s*(' + UNIT + r')'),
    ]
    candidate_matches = []
    sentence_meta = []
    for sentence_index, sentence in enumerate(re.finditer(r'[^。！？；;\n]+', block['text'])):
        sentence_meta.append(sentence.group())
        matches = sorted((m.start(), m.end(), kind, m)
                         for kind, pattern in patterns for m in re.finditer(pattern, sentence.group()))
        previous_end = 0
        for marker_start, marker_end, kind, match in matches:
            if marker_start < previous_end:
                continue
            prefix = sentence.group()[previous_end:marker_start]
            # A comma or connector belongs to neither adjacent assertion. Text after
            # the final predicate remains uncovered instead of inheriting its status.
            offset = max(prefix.rfind('，'), prefix.rfind(',')) + 1
            leading = prefix[offset:]
            trim = re.match(r'^[\s、]*(?:(?:并且|而且|同时|以及|且|并)[\s、]*)?', leading).end()
            local_start = previous_end + offset + trim
            start = sentence.start() + local_start
            end = sentence.start() + marker_end
            text = block['text'][start:end]
            leading_punctuation = len(text) - len(text.lstrip(' \t、，,'))
            if leading_punctuation:
                start += leading_punctuation
                text = text[leading_punctuation:]
            local_match = re.search(dict(patterns)[kind], text)
            candidate_matches.append((sentence_index, start, end, text, kind, local_match))
            previous_end = marker_end

    # The old extractor produced one claim per sentence. Preserve those IDs when
    # the old primary kind is still present, while assigning deterministic IDs to
    # additional assertions introduced by compound extraction.
    legacy_kind, legacy_id_index = {}, {}
    legacy_counter = 0
    for sentence_index, sentence_text in enumerate(sentence_meta):
        if re.search(patterns[0][1], sentence_text):
            legacy_kind[sentence_index] = 'growth'
        elif re.search(patterns[1][1], sentence_text):
            legacy_kind[sentence_index] = 'ranking'
        elif re.search(patterns[2][1], sentence_text):
            legacy_kind[sentence_index] = 'threshold'
        elif re.search(patterns[3][1], sentence_text):
            legacy_kind[sentence_index] = 'budget'
        elif re.search(patterns[4][1], sentence_text):
            legacy_kind[sentence_index] = 'quote'
        if legacy_kind.get(sentence_index):
            legacy_id_index[sentence_index] = legacy_counter
            legacy_counter += 1
    compound_counter, legacy_assigned = 0, set()

    # 同一段文本在一次抽取里会被反复求来源：算 source_text 时一次、按期间各一次、
    # 每个论断分支又一次、循环末尾还有一次。best_facts 每次都要遍历候选事实做
    # 子串判断，于是单段的开销按论断数线性放大。这里按 (文本, 期间) 记忆本轮结果——
    # 事实与索引在函数内是常量，记忆化不改变语义，只去掉重复计算。
    resolved = {}

    def resolve(text, period=None):
        key = (text, period)
        hit = resolved.get(key)
        if hit is None:
            hit = best_facts(text, facts, period=period, index=index)
            resolved[key] = hit
        return hit

    for sentence_index, start, end, text, marker_kind, match in candidate_matches:
        refs, spec, kind, issue = [], {}, None, ''
        source_text = text if resolve(text) else context
        if marker_kind == 'growth':
            growth = match
            kind = 'growth'
            comparison_explicit = bool(re.match(r'(?:较上期|环比|较上年|比上年|同比)', growth.group()))
            curr = resolve(source_text, '本期')
            prev = resolve(source_text, '上期')
            suggested = []
            if len(curr) == len(prev) == 1:
                refs = [prev[0]['id'], curr[0]['id']]
            else:
                issue = '未唯一确定同口径的上期与本期数据，请确认关联'
                # 只要有一侧唯一，就能用它的指标把另一侧补出来，作为人工候选；
                # 否则这条论断在界面上会变成"零选项"的死结（无法勾选、无法确认）。
                known = curr or prev
                if len(known) == 1:
                    suggested = growth_pair(known[0]['metric'], facts)
            # 方向词扩展：年报用「增加/减少/上升」，旧字典只有「增长/下降/持平」。
            # 同时用 number() 而非 float()，以支持千分位数值。
            direction_word = {'增长': 1, '增加': 1, '上升': 1,
                              '下降': -1, '减少': -1, '持平': 0}[growth[1]]
            spec = {'span': [growth.start(1), growth.end()],
                    'reported': float(number(growth[2])) if growth[2] else 0.0,
                    'qualitative': growth[2] is None, 'direction': direction_word}
            if not comparison_explicit:
                # “增加25%”没有明说相对哪一期，不能静默假设上期。
                # 保留抽取锚点，但必须由用户补全比较基准与两侧事实。
                spec['comparison_period_explicit'] = False
                refs = []
                issue = '增长率的比较基准未明确，请确认同口径的两期来源'
            if suggested:
                spec['suggested_refs'] = suggested
            if direction_word < 0:
                spec['reported'] = -abs(spec['reported'])
        elif marker_kind == 'ranking':
            rank = match
            kind = 'ranking'
            metric = rank[2]
            refs = ranking_cohort(metric, facts)
            spec = {'metric': metric, 'winners': rank[1].strip().split('、'), 'span': [rank.start(), rank.end()]}
            issue = '' if len(refs) >= 2 else '排名至少需要两个可比较对象'
        elif marker_kind == 'threshold':
            threshold = match
            kind = 'threshold'
            fs = resolve(source_text)
            refs = [fs[0]['id']] if len(fs) == 1 else []
            positive, negative, op = ('超过', '未超过', '>')
            if threshold[1] in ('不少于', '低于'):
                positive, negative, op = ('不少于', '低于', '>=')
            spec = {'limit': float(number(threshold[2])), 'unit': threshold[3], 'positive': positive,
                    'negative': negative, 'op': op, 'reported_positive': threshold[1] == positive,
                    'span': [threshold.start(1), threshold.end(1)]}
        elif marker_kind == 'budget':
            kind = 'threshold'
            # Resolve each side from an explicit metric phrase; both period and
            # metric are required so similarly named facts stay ambiguous.
            fs1, fs2 = resolve('本期支出'), resolve('本期预算')
            refs = [fs1[0]['id'], fs2[0]['id']] if len(fs1) == len(fs2) == 1 else []
            m = match
            spec = {'positive': '超过', 'negative': '未超过', 'op': '>', 'reported_positive': m[1] == '超过', 'span': [m.start(), m.end()]}
            spec['span'] = [m.start(1), m.end(1)]
        elif marker_kind == 'quote':
            quote = match
            kind = 'quote'
            fs = resolve(source_text)
            refs = [fs[0]['id']] if len(fs) == 1 else []
            spec = {'reported': float(number(quote[1])), 'unit': quote[2], 'span': [quote.start(1), quote.end(1)]}
        if kind:
            if not refs:
                issue = issue or '未找到唯一来源，需手动关联事实'
            if legacy_kind.get(sentence_index) == kind and sentence_index not in legacy_assigned:
                legacy_assigned.add(sentence_index)
                source_key = stable_id(block['file_id'], block['location'], kind, legacy_id_index[sentence_index])
            else:
                source_key = stable_id(block['file_id'], block['location'], 'compound-v2', compound_counter, kind)
            compound_counter += 1
            result.append({'id': source_key,
                           'file_id': block['file_id'], 'location': block['location'], 'label': block['label'],
                           'original': text, 'start': start, 'end': end,
                           'kind': kind, 'refs': refs, 'spec': spec, 'confirmed': False,
                           'extraction': '规则识别', 'issue': issue})
        context = text if resolve(text) else context
    return result


def unmatched_spans(blocks, claims):
    """Return uncovered prose, including gaps in otherwise recognized paragraphs."""
    result = []
    for block in blocks:
        ranges = sorted((c['start'], c['end']) for c in claims if c['kind'] != 'chart'
                        and c['file_id'] == block['file_id'] and c['location'] == block['location'])
        offset = 0
        for start, end in ranges + [(len(block['text']), len(block['text']))]:
            raw = block['text'][offset:start]
            text = raw.strip(' \t\r\n。！？；;，,、')
            if text and not re.fullmatch(r'(?:并且|而且|同时|以及|且|并)', text):
                leading = len(raw) - len(raw.lstrip(' \t\r\n。！？；;，,、'))
                result.append(dict(block, text=text, start=offset + leading, end=start))
            offset = max(offset, end)
    return result


def replace_span(text, span, replacement):
    return text[:span[0]] + replacement + text[span[1]:]


def check(claim, facts):
    byid = {f['id']: f for f in facts}
    out = {'claim_id': claim['id'], 'status': 'unverifiable', 'code': 'needs_review', 'expected': None, 'reason': '', 'evidence': []}
    try:
        refs = [byid[i] for i in claim['refs']]
        out['evidence'] = [{k: f.get(k) for k in ('id', 'subject', 'metric', 'period', 'value', 'unit', 'scope', 'sheet', 'cell')} for f in refs]
        if not refs or any(f.get('value') is None for f in refs):
            raise CheckError('missing_data', '来源缺失或数值不可计算')
        k, s, old = claim['kind'], claim['spec'], claim['original']
        expected, ok, calculation = old, False, ''
        if k == 'quote':
            if len(refs) != 1:
                raise CheckError(None, '数值引用必须关联一个事实')
            value = convert(refs[0]['value'], refs[0]['unit'], s['unit'])
            ok = value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == number(s['reported']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            expected = replace_span(old, s['span'], fmt(value))
            calculation = f"{refs[0]['value']} {refs[0]['unit']} = {fmt(value)} {s['unit']}"
        elif k == 'growth':
            if len(refs) != 2:
                raise CheckError(None, '增长率需要上期、本期两个事实')
            a, b = refs
            if any(a[x] != b[x] for x in ('subject', 'metric', 'scope')) or a['period'] != '上期' or b['period'] != '本期':
                raise CheckError('scope_mismatch', '增长率的主体、指标、口径或上期本期顺序不一致')
            before, after = number(a['value']), convert(b['value'], b['unit'], a['unit'])
            if before <= 0:
                raise CheckError('needs_review', '上期数值必须大于零；零值或负基期转人工复核')
            rate = (after - before) / before * 100
            rounded = rate.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            if s.get('qualitative'):
                direction = 1 if rate > 0 else -1 if rate < 0 else 0
                ok = direction == s['direction']
                wording = {1: '增长', -1: '下降', 0: '持平'}[direction]
            else:
                ok = rounded == number(s['reported']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                wording = '持平' if rounded == 0 else ('增长' if rounded > 0 else '下降') + fmt(abs(rounded)) + '%'
            expected = replace_span(old, s['span'], wording)
            calculation = f'({fmt(after)} − {fmt(before)}) / {fmt(before)} × 100 = {fmt(rate)}%'
        elif k == 'ranking':
            if len(refs) < 2:
                raise CheckError('source_conflict', '排名至少需要两个对象')
            first = refs[0]
            if any(any(f[x] != first[x] for x in ('metric', 'period', 'scope')) for f in refs):
                raise CheckError('scope_mismatch', '排名对象的指标、期间或统计口径不一致')
            if len({f['subject'] for f in refs}) != len(refs):
                raise CheckError('source_conflict', '排名对象重复，请核对比较集合')
            values = [convert(f['value'], f['unit'], first['unit']) for f in refs]
            winners = [f['subject'] for f, v in zip(refs, values) if v == max(values)]
            ok = set(winners) == set(s['winners'])
            target = '、'.join(winners) + s['metric'] + ('并列最高' if len(winners) > 1 else '最高')
            expected = replace_span(old, s['span'], target)
            calculation = '；'.join(f"{f['subject']} {fmt(v)}{first['unit']}" for f, v in zip(refs, values)) + '（仅针对已确认的比较集合）'
        elif k == 'threshold':
            if 'limit' in s:
                if len(refs) != 1:
                    raise CheckError(None, '固定阈值需要一个事实')
                value, limit, unit = convert(refs[0]['value'], refs[0]['unit'], s['unit']), number(s['limit']), s['unit']
            else:
                if len(refs) != 2:
                    raise CheckError(None, '支出与预算需要两个事实')
                a, b = refs
                if any(a[x] != b[x] for x in ('subject', 'period', 'scope')) or a['metric'] != '支出' or b['metric'] != '预算':
                    raise CheckError('scope_mismatch', '支出与预算的主体、期间、口径或指标不一致')
                value, limit, unit = number(a['value']), convert(b['value'], b['unit'], a['unit']), a['unit']
            truth = value > limit if s['op'] == '>' else value >= limit
            ok = truth == s['reported_positive']
            expected = replace_span(old, s['span'], s['positive'] if truth else s['negative'])
            calculation = f"{fmt(value)}{unit} {s['op']} {fmt(limit)}{unit} → {'成立' if truth else '不成立'}"
        elif k == 'chart':
            if len(refs) != len(s['values']):
                raise CheckError('source_conflict', '图表关联数量与原始类别不一致')
            values = [convert(f['value'], f['unit'], s['unit']) for f in refs]
            if any(f['metric'] != refs[0]['metric'] or f['scope'] != refs[0]['scope'] or f['subject'] != refs[0]['subject'] for f in refs):
                raise CheckError('scope_mismatch', '图表系列的指标、主体或口径不一致')
            if [f['period'] for f in refs] != s['categories']:
                raise CheckError('scope_mismatch', '图表类别与来源期间不匹配')
            ok = all(number(x) == y for x, y in zip(s['values'], values))
            expected = s['series'] + '：' + '，'.join(f'{c} {fmt(v)}{s["unit"]}' for c, v in zip(s['categories'], values))
            calculation = '按已确认的系列与类别重建原生图表数据'
        else:
            raise CheckError('needs_review', '不支持的论断类型')
        out.update(status='consistent' if ok else 'inconsistent',
                   code='consistent' if ok else 'inconsistent',
                   expected=old if ok else expected, reason=calculation)
    except (ValueError, KeyError, IndexError, InvalidOperation, TypeError) as exc:
        reason = str(exc)
        out['reason'] = reason
        # 分类取结构化错误码，不再比对提示文案。兜底为 missing_data 的理由：
        # 本项目里核验无法进行，主要就是"该有的来源没有或不可计算"；
        # 多给了来源（引用数量超出该论断所需）才单独归为 source_count。
        # 引用指向不存在的事实时同样归为 missing_data——那是最根本的缺失。
        code = getattr(exc, 'code', None)
        if code is None:
            code = 'missing_data'
            if claim['kind'] == 'quote':
                code = 'source_count' if len(claim['refs']) > 1 else 'missing_data'
            elif claim['kind'] == 'threshold' and 'limit' in claim.get('spec', {}):
                code = 'source_count' if len(claim['refs']) > 1 else 'missing_data'
            elif claim['kind'] in ('growth', 'chart'):
                expected = 2 if claim['kind'] == 'growth' else len(claim.get('spec', {}).get('values', []))
                code = 'source_count' if len(claim['refs']) > expected else 'missing_data'
        if any(i not in byid for i in claim['refs']):
            code = 'missing_data'
        out['code'] = code
    return out


def inspect(workspace):
    checks = [dict(check(c, workspace['facts']), confirmed=c['confirmed']) for c in workspace['claims']]
    summary = {'claims': len(checks), 'consistent': 0, 'inconsistent': 0, 'unverifiable': 0,
               'pending': 0, 'repairable': 0, 'documents': len(workspace['documents']), 'facts': len(workspace['facts'])}
    for r in checks:
        summary[r['status']] += 1
        summary['pending'] += not r['confirmed']
        summary['repairable'] += r['confirmed'] and r['status'] == 'inconsistent'
    return checks, summary
