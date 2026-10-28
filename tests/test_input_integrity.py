import copy

import fitz

from tower_rebuild.io import read_json
from tower_rebuild.native_bom import check_bom_observations, read_native_bom
from tower_rebuild.workflow import input_coverage, process_drawing


def row(page=1, length=1000, quantity=2, box=None):
    return {'mark': '101', 'specification': 'Q345L70X5', 'length_mm': length, 'quantity': quantity,
            'evidence': {'page': page, 'bbox': box or [.1, .2, .8, .3], 'text': 'native cells'}}


def test_conflicting_values_keep_all_rows_and_block_unique_mark_reduction():
    observations = [row(1), row(2, length=1250, quantity=3)]
    before = copy.deepcopy(observations)
    result = check_bom_observations(observations)
    assert result['status'] == 'blocked'
    assert result['issues'][0]['code'] == 'conflicting_bom_rows'
    assert result['issues'][0]['pages'] == [1, 2]
    assert result['issues'][0]['rows'] == before
    assert observations == before


def test_equal_cross_page_marks_are_not_assumed_one_quantity():
    result = check_bom_observations([row(1), row(2)])
    assert result['status'] == 'blocked' and result['row_observations'] == 2
    assert result['issues'][0]['code'] == 'duplicate_bom_scope_unresolved'


def test_same_physical_row_hit_twice_is_not_a_second_bom_row():
    assert check_bom_observations([row(), row()])['status'] == 'passed'
    result = check_bom_observations([row(), row(box=[.1, .5, .8, .6])])
    assert result['status'] == 'blocked'


def test_incomplete_native_table_is_not_treated_as_complete_bom():
    result = check_bom_observations([row()], ['one row missing quantity'])
    assert result['status'] == 'blocked'
    assert result['issues'][0]['code'] == 'native_table_parse_issue'


def test_mixed_vector_and_scan_pages_list_each_uncovered_page():
    result = input_coverage([
        {'page': 1, 'native_paths': 100, 'text_characters': 200, 'largest_raster_fraction': 0},
        {'page': 2, 'native_paths': 0, 'text_characters': 0, 'largest_raster_fraction': .9},
        {'page': 3, 'native_paths': 20, 'text_characters': 100, 'largest_raster_fraction': .8}])
    assert result['pages_with_coverage_issues'] == 2
    assert [issue['page'] for issue in result['issues']] == [2, 3]
    assert result['status'] == 'needs_review'


def make_bom_pdf(path, lengths):
    with fitz.open() as doc:
        for length in lengths:
            page = doc.new_page(width=500, height=300)
            xs, ys = [40, 110, 280, 360, 430], [40, 70, 100]
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            for column, text in enumerate(['编号', '规格', '长度', '数量']):
                page.insert_text((xs[column]+5, 59), text, fontname='china-s', fontsize=10)
            for column, text in enumerate(['101', 'Q345L70X5', str(length), '2']):
                page.insert_text((xs[column]+5, 89), text, fontsize=10)
        doc.save(path)


def test_actual_pdf_parser_preserves_conflicting_page_evidence(tmp_path):
    pdf = tmp_path / 'two-boms.pdf'
    make_bom_pdf(pdf, [1000, 1250])
    observations = []
    rows, regions, diagnostics = read_native_bom(pdf, row_observations=observations)
    assert len(observations) == 2
    assert [r['evidence']['page'] for r in observations] == [1, 2]
    assert [r['length_mm'] for r in observations] == [1000, 1250]
    assert rows[0].evidence.page == 1
    assert diagnostics
    assert check_bom_observations(observations, diagnostics)['status'] == 'blocked'


def test_actual_conflicting_pdf_stops_before_topology_and_preserves_report(tmp_path):
    pdf = tmp_path / 'two-boms.pdf'
    make_bom_pdf(pdf, [1000, 1250])
    result = process_drawing(pdf, tmp_path / 'out')
    assert result['status'] == 'unsupported'
    assert result['bom_integrity']['status'] == 'blocked'
    assert next(s for s in result['stages'] if s['id'] == 'topology')['status'] == 'pending'
    assert len(read_json(tmp_path / 'out/site/tables/bom-observations.json')) == 2
    assert '1250' in (tmp_path / 'out/input-review.md').read_text(encoding='utf-8')
    assert not (tmp_path / 'out/site/topology/model-preview.json').exists()
