import copy
from pathlib import Path

import pytest

from tower_rebuild.io import digest, file_digest, write_json
from tower_rebuild.model_dimension_audit import compare_model, polygon_measurements, validate_inputs


def inputs():
    axes = [([0, 0], [3, 0]), ([3, 0], [3, 4]), ([3, 4], [0, 0])]
    members = [{'id': f'm{i}', 'mark': str(i), 'axis_pt': {'a': a, 'b': b},
                'section': {'kind': 'angle', 'legs_mm': [5, 5], 'thickness_mm': 1},
                'bom_length_mm': length, 'projected_preview_length_mm': 99999,
                'overlap_status': 'candidate'} for i, ((a, b), length) in enumerate(zip(axes, (30, 40, 50)))]
    view = {'view_id': 'v1', 'title': 'test', 'bbox_pt': [0, 0, 3, 4],
            'scale': {'mm_per_pt': 10, 'basis': 'dimension_span_projection_candidate',
                      'evidence': [{'id': 'text0', 'ratio': 10}]},
            'members': members, 'plates': [], 'holes': [], 'issues': []}
    model = {'source_sha256': 'source', 'views': [view]}
    polygon = {'id': 'poly1', 'view_id': 'v1', 'association_status': 'projected_face_candidate',
               'edges': [{'member_ids': [f'm{i}'], 'marks': [str(i)], 'value_mm': n, 'text_id': f'text{i}',
                          'structural_a_pt': [999, 999], 'structural_b_pt': [111, 111], 'rounding_mm': .5}
                         for i, n in enumerate((30, 40, 50))]}
    audit = {'source_sha256': 'source', 'polygons': [polygon], 'annotations': [], 'findings': [],
             'span_bom_checks': [{'id': 'span1', 'view_id': 'v1', 'member_id': 'm0', 'mark': '0',
                                 'span_mm': 10, 'bom_length_mm': 30, 'status': 'basis_incomplete'}]}
    return model, audit


def test_lengths_come_from_geometry_and_partial_span_is_not_cut_length():
    model, audit = inputs(); report = compare_model(model, audit)
    member = next(r for r in report['rows'] if r['kind'] == 'member_bom')
    assert member['measured_mm'] == 30  # cached 99999 must never be trusted
    partial = next(r for r in report['rows'] if r['kind'] == 'member_initial_audit')
    assert partial['measured_mm'] == 30
    assert partial['reference_mm'] is None and partial['delta_mm'] is None
    assert partial['status'] == 'pending' and partial['original_span_mm'] == 10


def test_complete_audit_detects_changed_extrusion_without_mutating_initial_audit():
    model, audit = inputs(); original = copy.deepcopy(audit)
    audit['span_bom_checks'][0].update(computed_cut_length_mm=30, rounding_bound_mm=1)
    model['views'][0]['members'][0]['axis_pt']['b'] = [2, 0]
    before = copy.deepcopy(audit)
    row = next(r for r in compare_model(model, audit)['rows'] if r['kind'] == 'member_initial_audit')
    assert row['delta_mm'] == -10 and row['status'] == 'difference'
    assert audit == before and original['span_bom_checks'][0]['status'] == 'basis_incomplete'


def test_node_spans_reconstructed_from_model_axes_and_source_reuse_is_explicit():
    model, audit = inputs()
    rows = [r for r in compare_model(model, audit)['rows'] if r['kind'] == 'node_span']
    assert [r['measured_mm'] for r in rows] == pytest.approx([30, 40, 50])
    assert rows[0]['dependency']['reference_used_in_scale'] == ['text0']
    assert all(not r['dependency']['independent_engineering_validation'] for r in rows)
    model['views'][0]['members'][0]['axis_pt']['b'] = [1, 0]
    rows = [r for r in compare_model(model, audit)['rows'] if r['kind'] == 'node_span']
    assert rows[0]['measured_mm'] == 30 and rows[0]['geometry']['uses_axis_extension']


def test_ambiguous_axis_and_unmapped_polygons_stay_pending():
    model, audit = inputs(); view = model['views'][0]
    alternate = copy.deepcopy(view['members'][0]); alternate['id'] = 'other'
    alternate['axis_pt'] = {'a': [0, 1], 'b': [3, 1]}; view['members'].append(alternate)
    audit['polygons'][0]['edges'][0]['member_ids'].append('other')
    rows = [r for r in compare_model(model, audit)['rows'] if r['kind'] == 'node_span']
    assert all(r['measured_mm'] is None and r['status'] == 'pending' for r in rows)
    audit['polygons'][0]['association_status'] = 'ambiguous'
    assert polygon_measurements(audit['polygons'][0], {m['id']: m for m in view['members']}, 10) is None


def test_same_mark_different_observations_are_not_joined_by_mark():
    model, audit = inputs(); view = model['views'][0]
    view['members'][1]['mark'] = '0'
    audit['span_bom_checks'][0].update(computed_cut_length_mm=30)
    row = next(r for r in compare_model(model, audit)['rows'] if r['kind'] == 'member_initial_audit')
    assert row['measured_mm'] == 30 and row['object_id'] == 'm0'


def test_pending_hole_pitch_is_not_validated_against_nearby_numbers():
    model, audit = inputs(); view = model['views'][0]
    view['holes'] = [{'id': str(i), 'center_pt': p, 'diameter_mm': 2, 'member_ids': ['m0'],
                      'plate_ids': [], 'diameter_evidence': ['text0']} for i, p in enumerate(([0, 0], [2, 0]))]
    rows = compare_model(model, audit)['rows']; pitch = next(r for r in rows if r['kind'] == 'hole_distance')
    assert pitch['measured_mm'] == 20 and pitch['status'] == 'pending'
    assert all(r['status'] == 'input_reused' for r in rows if r['kind'] == 'hole_diameter')


def test_stale_source_and_audit_are_rejected(tmp_path):
    source = tmp_path / 'source.pdf'; source.write_bytes(b'sample')
    sha = file_digest(source); write_json(tmp_path / 'manifest.json', {'source': str(source), 'source_sha256': sha})
    names = ['topology/report.json', 'topology/views.json', 'topology/members.json', 'topology/geometry.json',
             'tables/bom.json', 'structure/primitives.json', 'structure/callouts.json']
    for name in names:
        write_json(tmp_path / 'site' / name, {})
    audit = {'source_sha256': sha, 'input_sha256': digest({str(Path(n)): file_digest(tmp_path / 'site' / n) for n in names})}
    model = {'source_sha256': sha, 'input_files': {}}
    validate_inputs(tmp_path, model, audit)
    write_json(tmp_path / 'site/tables/bom.json', {'changed': True})
    with pytest.raises(ValueError, match='stale'):
        validate_inputs(tmp_path, model, audit)
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='Source mismatch'):
        validate_inputs(tmp_path, model, audit)


@pytest.mark.parametrize('tolerance', [-1, float('nan'), float('inf')])
def test_invalid_diagnostic_tolerance_is_rejected(tolerance):
    with pytest.raises(ValueError):
        compare_model(*inputs(), tolerance)
