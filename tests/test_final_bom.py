import copy
import csv
import io
import math
from types import SimpleNamespace
from threading import RLock

import pytest

from tower_rebuild.final_bom import build_final_bom, publish_final_bom, publish_saved_run_bom, safe_csv
from tower_rebuild.io import write_json, file_digest, read_json
from tower_rebuild.assembly_projects import AssemblyProjects
from tower_rebuild.assembly_pipeline import publish_run_assembly
from test_assembly_projects import add_source


def fixture():
    bom = {'mark': '301', 'specification': 'Q345L75X6', 'length_mm': 100., 'quantity': 2, 'table_id': 't1'}
    source = {'id': 's1', 'name': 'sheet.pdf', 'bom': [bom], 'original_tables': [
        {'kind': 'bom', 'id': 't1', 'page': 1, 'rows': [['301', 'Q345L75X6', '100', '2', '1.23', '2.5', '端套']]}]}
    member = {'id': 'm1', 'mark': '301', 'a': [0, 0, 0], 'b': [110, 0, 0], 'endpoints_verified': True,
              'projected_preview_length_mm': 90, 'correction': {'original_length_mm': 90, 'corrected_length_mm': 999,
                                                              'added_mm': 20, 'trimmed_mm': 0}}
    face = {'id': 'f1', 'source_id': 's1', 'view_id': 'v1', 'title': '主结构图', 'units': 'mm',
            'members': [member], 'plates': [], 'edges': [{'id': 'e1'}]}
    snapshot = {'id': 'project', 'title': '当前模型', 'revision': 3, 'faces': [face],
                'scene': {'links': [{'a': 'e1', 'b': 'e1'}], 'poses': {}}, 'assessment': {}}
    return [source], snapshot


def test_final_geometry_not_cached_length_and_original_data_preserved():
    sources, snapshot = fixture()
    before = copy.deepcopy((sources, snapshot))
    report = build_final_bom(sources, snapshot)
    row = report['rows'][0]
    assert row['original'] == sources[0]['bom'][0]
    assert row['model_lengths_mm'] == [110]
    assert row['max_abs_difference_mm'] == 10
    assert row['unit_weight_kg'] == '1.23' and row['total_weight_kg'] == '2.5' and row['remarks'] == '端套'
    assert row['observations'][0]['original_model_length_mm'] == 90
    assert row['needs_attention'] and row['corrected_observation_count'] == 1
    assert (sources, snapshot) == before


def test_views_are_not_quantities_and_conflicting_lengths_are_not_averaged():
    sources, snapshot = fixture()
    second = copy.deepcopy(snapshot['faces'][0])
    second['id'] = 'f2'; second['members'][0]['b'] = [120, 0, 0]
    snapshot['faces'].append(second)
    row = build_final_bom(sources, snapshot)['rows'][0]
    assert row['model_lengths_mm'] == [110, 120]
    assert row['original']['quantity'] == 2 and row['observation_count'] == 2
    assert any('未取平均值' in r for r in row['attention_reasons'])


def test_cross_pdf_marks_do_not_merge():
    sources, snapshot = fixture()
    other = copy.deepcopy(sources[0]); other['id'] = 's2'; other['name'] = 'other.pdf'; sources.append(other)
    rows = build_final_bom(sources, snapshot)['rows']
    assert len(rows) == 2
    assert next(r for r in rows if r['source_id'] == 's2')['model_lengths_mm'] == []


@pytest.mark.parametrize('kind', ['unscaled', 'missing', 'zero', 'invalid'])
def test_missing_or_unmeasurable_lengths_stay_empty(kind):
    sources, snapshot = fixture()
    face = snapshot['faces'][0]
    if kind == 'unscaled': face['units'] = 'pdf_pt'
    if kind == 'missing': face['members'] = []
    if kind == 'zero': face['members'][0]['b'] = [0, 0, 0]
    if kind == 'invalid': face['members'][0]['a'] = [1, 2]
    row = build_final_bom(sources, snapshot)['rows'][0]
    assert row['model_lengths_mm'] == [] and row['max_abs_difference_mm'] is None and row['needs_attention']


def test_unlisted_model_component_is_not_lost():
    sources, snapshot = fixture(); snapshot['faces'][0]['members'][0]['mark'] = '999'
    rows = build_final_bom(sources, snapshot)['rows']
    assert len(rows) == 2
    unknown = next(r for r in rows if r['mark'] == '999')
    assert unknown['original']['quantity'] is None
    assert '模型件号在原图 BOM 中不存在' in unknown['attention_reasons']


def test_plate_length_has_different_basis_and_no_false_stock_delta():
    sources, snapshot = fixture(); face = snapshot['faces'][0]
    face['members'] = []; face['plates'] = [{'id': 'p1', 'mark': '301', 'polygon': [[0,0,0],[150,0,0],[150,50,0],[0,50,0]]}]
    sources[0]['bom'][0]['specification'] = 'Q345-6X50'
    row = build_final_bom(sources, snapshot)['rows'][0]
    assert row['model_lengths_mm'] == [150] and row['max_abs_difference_mm'] is None
    assert row['observations'][0]['length_basis'] == '板形包络长边'
    assert row['needs_attention']
    snapshot['scene']['face_corrections'] = {'f1': {}}
    face['plates'][0]['polygon'] = None
    report = build_final_bom(sources, snapshot)
    assert report['counts']['corrected_observations'] == 0
    assert report['counts']['without_measurable_length'] == 1


def test_tolerance_and_positive_no_attention_case():
    sources, snapshot = fixture(); member = snapshot['faces'][0]['members'][0]
    member.pop('correction'); member['b'] = [103, 0, 0]
    sources[0]['bom'][0]['quantity'] = 1; sources[0]['original_tables'][0]['rows'][0][3] = '1'
    assert not build_final_bom(sources, snapshot)['rows'][0]['needs_attention']
    member['b'][0] = 103.001
    assert build_final_bom(sources, snapshot)['rows'][0]['needs_attention']


def test_bad_seams_and_unavailable_sources_are_visible():
    sources, snapshot = fixture()
    snapshot['assessment']['links'] = [{'a': 'e1', 'b': 'e1', 'valid': False}]
    snapshot['sources'] = [{'id': 'waiting', 'name': 'waiting.pdf'}]
    report = build_final_bom(sources, snapshot)
    assert any('接缝' in r for r in report['rows'][0]['attention_reasons'])
    assert 'waiting.pdf' in report['warnings'][0]


def test_uncertain_original_extras_are_not_attached_to_wrong_bom():
    sources, snapshot = fixture()
    sources[0]['original_tables'][0]['rows'][0][2] = '101'
    row = build_final_bom(sources, snapshot)['rows'][0]
    assert row['remarks'] is None and row['original_table_evidence'][0]['cells'][6] == '端套'
    assert row['needs_attention']


def test_merged_raw_rows_and_extra_ambiguity():
    sources, snapshot = fixture()
    sources[0]['original_tables'][0]['rows'] = [['301\n302', 'Q345L75X6\nQ345L75X6', '100\n120', '2\n1', '1.23\n2.4', '2.5\n2.4', '共用备注']]
    row = build_final_bom(sources, snapshot)['rows'][0]
    assert row['unit_weight_kg'] == '1.23' and row['remarks'] is None
    assert row['original_table_evidence'][0]['cells'][-1] == '共用备注'
    assert any('未唯一对应' in r for r in row['attention_reasons'])


def test_safe_html_csv_and_attention_column_last(tmp_path):
    sources, snapshot = fixture(); sources[0]['name'] = '<img onerror=alert(1)>'
    sources[0]['original_tables'][0]['rows'][0][6] = '=1+2'
    report = publish_final_bom(build_final_bom(sources, snapshot), tmp_path)
    text = (tmp_path/'index.html').read_text(encoding='utf8')
    assert '<img onerror' not in text and '&lt;img' in text
    rows = list(csv.reader(io.StringIO((tmp_path/'final-bom.csv').read_text(encoding='utf-8-sig'))))
    assert rows[0][-1] == '是否需要人工关注' and rows[1][-1] == '是'
    assert rows[1][7] == "'=1+2"
    assert read_json(tmp_path/'final-bom.json')['rows'][0]['remarks'] == '=1+2'
    assert safe_csv('  @SUM(1)') == "'  @SUM(1)" and safe_csv(-2.) == '-2'


def test_current_project_export_remeasures_scale_and_undo_without_moving(tmp_path):
    jobs = SimpleNamespace(root=tmp_path, jobs={}, lock=RLock()); add_source(jobs, 'one')
    p = AssemblyProjects(jobs); aid = p.create(['one'])['id']; current = p.snapshot(aid, True)
    before = copy.deepcopy(current['scene'])
    first = p.export_final_bom(aid)
    assert first['rows'][0]['model_lengths_mm'] == [1000]
    assert p.snapshot(aid, True)['scene'] == before
    changed = p.mutate(aid, {'action': 'scale', 'revision': current['revision'], 'face_id': current['faces'][0]['id'], 'mm_per_pt': 2})
    second = p.export_final_bom(aid)
    assert second['rows'][0]['model_lengths_mm'] == [2000]
    assert first['basis_signature'] != second['basis_signature']
    moved = p.mutate(aid, {'action': 'pose', 'revision': changed['revision'], 'face_id': current['faces'][0]['id'],
                         'pose': {'position': [123, 456, 789], 'quaternion': [0, 0, 0, 1]}})
    p.export_final_bom(aid)
    p.mutate(aid, {'action': 'undo', 'revision': moved['revision']})
    third = p.export_final_bom(aid)
    assert third['rows'][0]['model_lengths_mm'] == [2000]
    assert p.snapshot(aid, True)['scene']['poses'] == changed['scene']['poses']


def test_static_export_integrated_and_refuses_stale_final_snapshot(tmp_path):
    jobs = SimpleNamespace(root=tmp_path, jobs={}); add_source(jobs, 'one')
    run = tmp_path/'one/run'; pdf = tmp_path/'one/source.pdf'
    write_json(run/'manifest.json', {'source': str(pdf), 'source_sha256': file_digest(pdf)})
    publish_run_assembly(run)
    assert (run/'site/final-bom/index.html').is_file()
    assert read_json(run/'site/assembly/drawing-links.json')['links']['finalbom'] == '../final-bom/'
    snapshot = (run/'site/assembly/assembly.json').read_bytes()
    publish_saved_run_bom(run)
    assert (run/'site/assembly/assembly.json').read_bytes() == snapshot
    write_json(run/'site/topology/views.json', [])
    with pytest.raises(ValueError, match='过期'):
        publish_saved_run_bom(run)
