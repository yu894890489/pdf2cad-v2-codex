import copy
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import ezdxf
import pytest

from tower_rebuild.final_bom import build_final_bom
from tower_rebuild.io import write_json, read_json, file_digest
from tower_rebuild import part_drawings as cad
from test_final_bom import fixture
from test_workflow import studio, call
from test_assembly_projects import add_source


def data():
    sources, snapshot = fixture()
    return snapshot, build_final_bom(sources, snapshot)


def test_model_length_and_specification_drive_separate_variants():
    sources, snapshot = fixture()
    face = copy.deepcopy(snapshot['faces'][0]);face['id'] = 'f2';face['members'][0]['b'] = [120,0,0]
    snapshot['faces'].append(face)
    parts, omitted = cad.build_parts(snapshot, build_final_bom(sources, snapshot))
    assert sorted(p['geometry']['length_mm'] for p in parts) == [110,120]
    assert not omitted and all(p['bom_length_mm'] == 100 for p in parts)
    assert len({p['filename'] for p in parts}) == 2


def test_same_geometry_merges_observations_not_original_quantity():
    sources, snapshot = fixture()
    face = copy.deepcopy(snapshot['faces'][0]);face['id'] = 'f2';snapshot['faces'].append(face)
    parts, _ = cad.build_parts(snapshot, build_final_bom(sources, snapshot))
    assert len(parts) == 1 and len(parts[0]['observations']) == 2 and parts[0]['bom_quantity'] == 2


def test_missing_geometry_not_replaced_with_bom_blank():
    sources, snapshot = fixture();snapshot['faces'][0]['units'] = 'pdf_pt'
    parts, omitted = cad.build_parts(snapshot, build_final_bom(sources, snapshot))
    assert not parts and '无可量取' in omitted[0]['reason']


def test_plate_normalization_keeps_real_shape_and_only_supported_holes():
    item = {'polygon': [[10,20,0],[110,20,0],[110,70,0],[10,70,0]], 'drill_holes_local': [
        {'id': 'ok', 'center': [40,40,0], 'diameter_mm': 10, 'evidence':['标注10']},
        {'id': 'outside', 'center':[200,200,0], 'diameter_mm':10, 'evidence':['标注10']},
        {'id': 'unknown', 'center':[80,50,0], 'diameter_mm':10}]}
    ring, holes, omitted = cad.normalized_plate(item)
    assert sorted(ring) == [[0,0],[0,50],[100,0],[100,50]]
    assert holes == [{'center':[30,20], 'diameter_mm':10, 'evidence':['标注10']}]
    assert omitted == ['outside','unknown']
    bad = {'polygon':[[0,0,0],[100,100,0],[0,100,0],[100,0,0]]}
    with pytest.raises(ValueError, match='自交'):cad.normalized_plate(bad)


def test_dxf_native_geometry_dimensions_units_and_no_invented_holes(tmp_path):
    snapshot, bom = data(); part = cad.build_parts(snapshot, bom)[0][0]
    original = copy.deepcopy(part)
    path = tmp_path/'part.dxf';cad.write_part_dxf(part, path)
    doc = ezdxf.readfile(path);space = doc.modelspace()
    assert doc.units == 4 and not doc.audit().has_errors
    assert doc.styles.get('PART_TEXT').dxf.font == 'simhei.ttf'
    outlines = [e for e in space if e.dxf.layer=='PART_OUTLINE']
    assert len(outlines) == 2
    assert max(p[0] for p in outlines[0].get_points('xy')) == 110
    assert len([e for e in space if e.dxftype() == 'DIMENSION']) == 5
    assert not list(space.query('CIRCLE'))
    assert any('人工关注' in e.text for e in space.query('MTEXT'))
    assert part == original


def test_no_converter_still_reports_dxf_and_never_claims_dwg(tmp_path):
    snapshot, bom = data()
    report = cad.publish_part_drawings(snapshot, bom, tmp_path, converter=False)
    assert report['status'] == 'needs_converter'
    assert report['counts']['verified_dwg'] == 0
    assert report['parts'][0]['dxf'] and not report['parts'][0].get('dwg')
    with zipfile.ZipFile(tmp_path/'parts-dwg.zip') as archive:
        assert not any(n.endswith('.dwg') for n in archive.namelist())


def test_conversion_failure_remains_visible(tmp_path, monkeypatch):
    snapshot, bom = data()
    def fail(*args):raise ValueError('roundtrip mismatch')
    monkeypatch.setattr(cad, 'convert_and_verify', fail)
    report = cad.publish_part_drawings(snapshot, bom, tmp_path, converter=Path('accoreconsole.exe'))
    assert report['status'] == 'blocked'
    assert report['parts'][0]['error'] == 'roundtrip mismatch'
    assert report['counts']['verified_dwg'] == 0


def test_extension_is_not_enough_for_dwg_validation(tmp_path, monkeypatch):
    snapshot, bom = data();part = cad.build_parts(snapshot, bom)[0][0]
    source = tmp_path/'part.dxf';cad.write_part_dxf(part, source)
    def fake_console(executable, input_file, commands, folder, log):
        (folder/'output.dwg').write_bytes(b'not-a-dwg'*40)
    monkeypatch.setattr(cad, 'run_console', fake_console)
    with pytest.raises(ValueError, match='有效'):
        cad.convert_and_verify(source, tmp_path/'out.dwg', Path('accoreconsole.exe'))
    assert not (tmp_path/'out.dwg').exists()


def test_output_text_and_filenames_cannot_inject_commands():
    assert cad.safe_name('../weird/"\nname') == 'weird_name'
    assert '\\{' in cad.clean_text('{hello}')
    assert '\n' not in cad.clean_text('one\ntwo')
    with pytest.raises(ValueError):cad.quote_path(Path('bad"path'))


def test_rigid_placement_does_not_stale_part_geometry():
    snapshot, _ = data();key = cad.model_basis(snapshot)
    snapshot['scene']['poses'] = {'f1': {'position':[100,100,0]}}
    assert cad.model_basis(snapshot) == key
    snapshot['faces'][0]['members'][0]['b'][0] += 1
    assert cad.model_basis(snapshot) != key


def test_background_http_auth_progress_and_file_whitelist(studio, monkeypatch):
    base, store = studio
    monkeypatch.setattr(cad, 'find_accoreconsole', lambda: None)
    add_source(store, 'cad-test')
    site = store.root/'cad-test/run/site'
    write_json(site/'tables/bom.json', [{'mark':str(i),'specification':'Q345L75X6','length_mm':1000,'quantity':1} for i in range(4)])
    model = read_json(site/'topology/model-preview.json')
    model['input_files']['tables/bom.json'] = file_digest(site/'tables/bom.json')
    write_json(site/'topology/model-preview.json', model)
    project = store.assemblies.create(['cad-test']);aid = project['id']
    endpoint = base+f'/api/assemblies/{aid}/part-drawings'
    status = call(endpoint)
    payload = json.dumps({'revision':status['current_revision']}).encode()
    with pytest.raises(HTTPError) as error:call(endpoint, payload)
    assert error.value.code == 403
    token = call(base+'/api/session')['token']
    headers = {'X-Upload-Token':token, 'Content-Type':'application/json'}
    with pytest.raises(HTTPError) as error:call(endpoint, b'{"revision":"bad"}', headers)
    assert error.value.code == 400
    original = store.assemblies.snapshot(aid, True)['scene']
    response = call(endpoint, payload, headers)
    assert response['status'] in {'queued','running'}
    deadline = time.monotonic()+25
    while time.monotonic()<deadline:
        status = call(endpoint)
        if status['status'] not in {'queued','running'}:break
        time.sleep(.1)
    assert status['status'] == 'needs_converter' and status['completed'] == 4
    assert store.assemblies.snapshot(aid, True)['scene'] == original
    url = base+status['result_url'].replace('index.html','')
    report = call(url+'report.json')
    with urlopen(url+report['parts'][0]['dxf']) as r:
        assert r.status == 200 and 'attachment' in r.headers['Content-Disposition']
    for name in ['../../current.json','logs/any.log','arbitrary.dwg']:
        with pytest.raises(HTTPError):urlopen(url+name)
