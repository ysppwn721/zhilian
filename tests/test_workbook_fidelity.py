"""事实表更新必须保留图表等未建模部件（openpyxl 的 load+save 会静默丢弃）。

背景：office.update_workbook 原先用 openpyxl load_workbook → 改单元格 → save。
openpyxl 只建模它支持的部分，**图表、条件格式、数据透视表、图片在保存时全部丢失**，
而导出接口返回成功、没有任何警告。中小企业的事实表常自带柱状图/饼图，用户一打开
发现图没了就会判定软件不可用。

现改为 xlsx（zip）级定点改写：只替换目标单元格的 <v>，其余条目原样拷贝。
本测试用真实带图表的 xlsx 验证"图表仍在"与"数值确实被改"。
"""
from __future__ import annotations

import zipfile

import pytest
from openpyxl import load_workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.styles import PatternFill

from zhilian.office import update_workbook


def make_book(path, with_charts=True, with_fill=True):
    wb = load_workbook() if False else None  # 显式建新簿，避免依赖模板
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = '事实表'
    ws.append(['事实ID', '主体', '指标', '期间', '数值', '单位', '统计口径'])
    ws.append(['sales_current', '总计', '销售额', '本期', 125, '万元', '演示业务'])
    ws.append(['budget', '总计', '预算', '本期', 100, '万元', '演示项目'])
    if with_fill:
        ws['E2'].fill = PatternFill('solid', fgColor='FFEEAA')
    if with_charts:
        bar = BarChart()
        bar.title = '销售额'
        bar.add_data(Reference(ws, min_col=5, min_row=1, max_row=3), titles_from_data=True)
        ws.add_chart(bar, 'H2')
        pie = PieChart()
        pie.title = '预算占比'
        pie.add_data(Reference(ws, min_col=5, min_row=1, max_row=3), titles_from_data=True)
        ws.add_chart(pie, 'H18')
    wb.save(path)
    return path


def chart_parts(path):
    with zipfile.ZipFile(path) as z:
        return sorted(n for n in z.namelist() if n.startswith('xl/charts/') and n.endswith('.xml'))


def sheet_xml(path, name='xl/worksheets/sheet1.xml'):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode('utf-8')


def test_update_workbook_keeps_charts_and_styles(tmp_path):
    src = make_book(tmp_path / 'src.xlsx')
    before_charts = chart_parts(src)
    assert len(before_charts) == 2, '测试夹具应含柱状图与饼图各一个'

    dst = tmp_path / 'out.xlsx'
    facts = [{'id': 'sales_current', 'sheet': '事实表', 'cell': 'E2'},
             {'id': 'budget', 'sheet': '事实表', 'cell': 'E3'}]
    update_workbook(src, dst, facts, {'sales_current': 90, 'budget': 110})

    # 1) 图表必须还在——这正是 openpyxl load+save 会丢掉的东西
    after_charts = chart_parts(dst)
    assert after_charts == before_charts, 'Excel 图表在更新后丢失'

    # 2) 数值必须真的被改
    wb = load_workbook(dst)
    ws = wb['事实表']
    assert float(ws['E2'].value) == 90.0
    assert float(ws['E3'].value) == 110.0
    wb.close()

    # 3) 单元格样式也应当保留：填充色记录在 xl/styles.xml（sheet XML 里只存样式索引）
    with zipfile.ZipFile(dst) as z:
        styles = z.read('xl/styles.xml').decode('utf-8')
    assert 'FFEEAA' in styles, '单元格样式在更新后丢失'
    assert 's="' in sheet_xml(dst), '单元格的样式索引被抹掉了'


def test_update_workbook_does_not_touch_unrelated_cells(tmp_path):
    src = make_book(tmp_path / 'src.xlsx', with_charts=False, with_fill=False)
    dst = tmp_path / 'out.xlsx'
    facts = [{'id': 'sales_current', 'sheet': '事实表', 'cell': 'E2'}]
    update_workbook(src, dst, facts, {'sales_current': 77})
    wb = load_workbook(dst)
    ws = wb['事实表']
    assert float(ws['E2'].value) == 77.0
    assert float(ws['E3'].value) == 100.0     # 未列入 changes，必须原样
    assert ws['A2'].value == 'sales_current'
    wb.close()


def test_update_workbook_without_changes_copies_file(tmp_path):
    src = make_book(tmp_path / 'src.xlsx')
    dst = tmp_path / 'out.xlsx'
    update_workbook(src, dst, [{'id': 'sales_current', 'sheet': '事实表', 'cell': 'E2'}], {})
    assert chart_parts(dst) == chart_parts(src)


def test_update_workbook_rejects_unresolvable_target(tmp_path):
    """宁可报错，也不要静默产出残缺文件。"""
    src = make_book(tmp_path / 'src.xlsx')
    dst = tmp_path / 'out.xlsx'
    with pytest.raises(ValueError):
        update_workbook(src, dst, [{'id': 'x', 'sheet': '不存在的表', 'cell': 'E2'}], {'x': 1})
    with pytest.raises(ValueError):
        update_workbook(src, dst, [{'id': 'x', 'sheet': '事实表', 'cell': 'ZZ99'}], {'x': 1})
    assert not dst.exists(), '失败时不应留下半成品文件'
