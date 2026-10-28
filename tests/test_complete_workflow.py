from pathlib import Path

import fitz
import httpx
import pytest

from tower_rebuild.final_audit import final_check
from tower_rebuild.io import digest, file_digest, read_json, write_json
from tower_rebuild.schema import Evidence
from tower_rebuild.studio import JobStore
from tower_rebuild.vision_review import ViewReview, VisionFinding, review_with_vision
from tower_rebuild.workflow_settings import WorkflowSettings, VisionReviewSettings


def vision_run(root):
    pdf = root / 'source.pdf'
    with fitz.open() as doc:
        doc.new_page(width=200, height=200)
        doc.save(pdf)
    write_json(root / 'manifest.json', {'source': str(pdf), 'source_sha256': file_digest(pdf)})
    views = [{'id': f'v{i}', 'page': 1, 'bbox_pt': [20, 20, 180, 180], 'title': f'view{i}'} for i in range(2)]
    write_json(root / 'site/topology/views.json', views)
    write_json(root / 'site/topology/model-preview.json', {'views': [
        {'view_id': v['id'], 'members': [{'id': v['id'] + '_member'}], 'plates': [], 'holes': []} for v in views]})
    write_json(root / 'site/tables/bom.json', [])
    write_json(root / 'site/audit/report.json', {'findings': []})


def enabled_options():
    return VisionReviewSettings(enabled=True, service={'model': 'test-vision'})


def test_vision_is_off_by_default_and_never_contacts_a_service(tmp_path):
    vision_run(tmp_path)
    def forbidden(*args):
        raise AssertionError('Disabled vision must not contact a service')
    report = review_with_vision(tmp_path, VisionReviewSettings(), forbidden)
    assert report['status'] == 'disabled' and not report['engineering_accuracy_verified']


@pytest.mark.parametrize('invalid', ['unknown_id', 'other_page', 'outside_crop'])
def test_unbound_visual_findings_cannot_enter_the_report(tmp_path, invalid):
    vision_run(tmp_path)
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def ask(self, stage, *args):
            vid = stage.split('-')[-1]
            return ViewReview(findings=[VisionFinding(category='member', severity='uncertain',
                object_ids=['ghost'] if invalid == 'unknown_id' else [vid + '_member'],
                observation='unclear member', suggested_check='check original drawing',
                evidence=Evidence(page=2 if invalid == 'other_page' else 1,
                                  bbox=(.01,.01,.02,.02) if invalid == 'outside_crop' else (.2,.2,.3,.3), text='evidence'))])
    report = review_with_vision(tmp_path, enabled_options(), Client)
    assert report['status'] == 'partial' and report['counts']['findings'] == 0
    assert all(v['status'] == 'unavailable' for v in report['views'])


def test_empty_visual_findings_are_advisory_and_do_not_approve_geometry(tmp_path):
    vision_run(tmp_path); before = file_digest(tmp_path / 'site/topology/model-preview.json')
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def ask(self, *args): return ViewReview()
    report = review_with_vision(tmp_path, enabled_options(), Client)
    assert report['status'] == 'completed_advisory'
    assert not report['engineering_accuracy_verified']
    assert before == file_digest(tmp_path / 'site/topology/model-preview.json')


def test_unavailable_vision_stops_repeated_calls_and_preserves_native_results(tmp_path):
    vision_run(tmp_path); calls = []
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def ask(self, *args):
            calls.append(1)
            raise httpx.ConnectError('local service unavailable')
    report = review_with_vision(tmp_path, enabled_options(), Client)
    assert len(calls) == 1 and report['views'][1]['status'] == 'not_reviewed'
    assert (tmp_path / 'site/topology/model-preview.json').exists()


def test_workflow_configuration_rejects_inline_keys_and_missing_model():
    with pytest.raises(ValueError):
        WorkflowSettings.model_validate({'vision_review': {'enabled': True}})
    with pytest.raises(ValueError):
        WorkflowSettings.model_validate({'vision_review': {'service': {'api_key': 'do-not-store'}}})
    assert not WorkflowSettings().vision_review.enabled


def test_retry_keeps_the_original_configuration_snapshot(tmp_path, monkeypatch):
    store = JobStore(tmp_path, {'name': 'original profile'})
    monkeypatch.setattr(store.executor, 'submit', lambda *args: None)
    try:
        jid, folder = store.reserve('test.pdf'); (folder / 'source.pdf').write_bytes(b'%PDF sample')
        store.submit(jid, 'test.pdf'); store.jobs[jid]['status'] = 'needs_review'
        store.config.name = 'changed profile'
        retried = store.retry(jid)
        assert read_json(store.root / retried['id'] / 'workflow-config.json')['name'] == 'original profile'
    finally:
        store.close()


def final_run(root):
    source = root / 'source.pdf'; source.write_bytes(b'source bytes'); sha = file_digest(source)
    write_json(root / 'manifest.json', {'source': str(source), 'source_sha256': sha})
    site = root / 'site'
    base = ['topology/report.json', 'topology/views.json', 'topology/members.json', 'topology/geometry.json',
            'tables/bom.json', 'structure/primitives.json', 'structure/callouts.json']
    for name in base:
        write_json(site / name, [] if name != 'topology/report.json' else {})
    write_json(site / 'summary.json', {'title': 'fixture', 'checks': {'preserved': True}})
    write_json(site / 'tables/bom-check.json', {'status': 'passed'})
    write_json(site / 'topology/connections.json', [{'id': 'c1', 'status': 'needs_review'}])
    write_json(site / 'audit/report.json', {'source_sha256': sha, 'findings': [], 'span_bom_checks': [],
               'input_sha256': digest({str(Path(n)): file_digest(site / n) for n in base})})
    write_json(site / 'topology/model-preview.json', {'source_sha256': sha, 'input_files': {},
               'views': [{'members': [{'id': 'm1'}], 'plates': [], 'holes': [{'id': 'h1'}]}]})
    write_json(site / 'topology/final-check.json', {'counts': {'angle_models': 1, 'plate_polygons': 0,
               'plate_shapes_pending': 1, 'member_incomplete': 1, 'hole_projections': 1,
               'explicit_hole_diameters': 0, 'hole_owner_pending': 1},
               'missing_member_marks': ['10'], 'missing_plate_marks': [], 'unresolved_small_arc_ids': []})
    write_json(site / 'model-audit/report.json', {'source_sha256': sha, 'rows': [
        {'id': 'difference1', 'kind': 'member_bom', 'status': 'difference'}],
        'input_files': {n: file_digest(site / n) for n in ('audit/report.json', 'topology/model-preview.json')}})
    for folder in ('topology', 'audit', 'model-audit'):
        (site / folder / 'index.html').write_text('<header></header>', encoding='utf-8')


def test_generated_model_is_not_promoted_to_final_accuracy(tmp_path):
    final_run(tmp_path); report = final_check(tmp_path)
    assert report['model_generated'] and report['status'] == 'needs_review'
    assert not report['engineering_accuracy_verified']
    checks = {c['id']: c for c in report['checks']}
    assert checks['marks']['status'] == 'fail'
    assert checks['model_dimensions']['status'] == 'fail'
    assert checks['quantities']['status'] == checks['assembly']['status'] == 'pending'
    assert checks['vision']['status'] == 'not_requested'


def test_final_check_rejects_a_stale_dimension_comparison(tmp_path):
    final_run(tmp_path)
    model = tmp_path / 'site/topology/model-preview.json'
    model.write_text(model.read_text() + '\n')
    with pytest.raises(ValueError, match='过期'):
        final_check(tmp_path)


def test_final_model_generation_reads_geometry_inventory_not_a_cached_count(tmp_path):
    final_run(tmp_path)
    model_path = tmp_path / 'site/topology/model-preview.json'
    model = read_json(model_path); model['views'] = []; write_json(model_path, model)
    comparison_path = tmp_path / 'site/model-audit/report.json'
    comparison = read_json(comparison_path)
    comparison['input_files']['topology/model-preview.json'] = file_digest(model_path)
    write_json(comparison_path, comparison)
    report = final_check(tmp_path)
    assert not report['model_generated'] and report['status'] == 'no_model'
    assert next(c for c in report['checks'] if c['id'] == 'model_inventory')['status'] == 'fail'


def test_circle_mark_retains_suffix_and_front_rear_side():
    from tower_rebuild.primitives import associate_callouts
    primitives = [{'id': 'circle', 'page': 1, 'part': 'structure', 'primitive_type': 'circle',
                   'geometry': {'center': [20, 20], 'radius': 10}, 'bbox_pt': [10,10,30,30]},
                  {'id': 'text', 'page': 1, 'part': 'structure', 'primitive_type': 'text',
                   'text': '后509B', 'bbox_pt': [14,17,26,23]}]
    bom = [{'mark': '509B', 'classification': {'kind': 'angle'}}]
    mark = associate_callouts(primitives, bom)[0]['marks'][0]
    assert mark['mark'] == '509B' and mark['side'] == '后' and mark['bom_found']


def test_doctor_uses_a_real_image_without_putting_the_answer_in_prompt(monkeypatch):
    from tower_rebuild.doctor import doctor, ProbeAnswer
    from PIL import Image
    import tower_rebuild.doctor as module
    monkeypatch.setattr(module.secrets, 'randbelow', lambda limit: 17)
    monkeypatch.setattr(module.shutil, 'which', lambda name: None)
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def ask(self, stage, schema, prompt, images, **kwargs):
            assert '0017' not in prompt and kwargs['use_cache'] is False
            with Image.open(images[0]) as image:
                assert image.size == (500, 180) and image.getextrema()[0] == (0, 255)
            return ProbeAnswer(digits='0017')
    result = doctor({'vision_review': enabled_options().model_dump()}, True, Client)
    assert result['vision_probe']['status'] == 'passed'
    assert not result['capabilities']['verified_automatic_spatial_assembly']


def test_doctor_rejects_wrong_visual_answer(monkeypatch):
    from tower_rebuild.doctor import doctor, ProbeAnswer
    import tower_rebuild.doctor as module
    monkeypatch.setattr(module.secrets, 'randbelow', lambda limit: 17)
    monkeypatch.setattr(module.shutil, 'which', lambda name: None)
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def ask(self, *args, **kwargs): return ProbeAnswer(digits='0018')
    assert doctor({'vision_review': enabled_options().model_dump()}, True, Client)['vision_probe']['status'] == 'failed'
