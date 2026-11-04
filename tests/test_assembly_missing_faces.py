import copy
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from tower_rebuild.assembly import pose_matrix, transformed
from tower_rebuild.assembly_fitting import fit_tapered_faces
from tower_rebuild.assembly_missing_faces import infer_missing_face, native_axis_evidence, missing_face_candidates, missing_face_options
from tower_rebuild.assembly_projects import AssemblyProjects, AssemblyConflict
from tower_rebuild.io import file_digest, read_json, write_json
from test_assembly_fitting import tapered_fixture
from test_assembly_projects import add_source


@pytest.fixture(scope='module')
def fixture():
    faces,matches,parallel,scene=tapered_fixture()
    for face in faces:face['view_id']=face['id']
    scene=fit_tapered_faces(faces,matches,parallel,scene,'negative_x')
    return faces,matches,parallel,scene


def test_prediction_uses_neighbors_not_held_out_coordinates(fixture):
    faces,_,_,scene=fixture
    before=copy.deepcopy([faces,scene])
    original=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    assert original['prediction']['planarity_max_mm']<.001
    assert sorted(original['prediction']['widths_mm'])==pytest.approx([200,500],abs=.01)
    assert original['original_fit']['max_gap_mm']<.01
    damaged=copy.deepcopy(faces)
    for edge in next(f for f in damaged if f['id']=='bridge2')['edges']:
        edge['b']=((np.asarray(edge['a'])+np.asarray(edge['b']))/2).tolist()
        edge['length']/=2
    check=infer_missing_face(damaged,scene,'bridge2',[],'fixture')
    assert check['prediction']==original['prediction']
    assert check['original_fit']['max_gap_mm']>200
    assert check['original_fit']['rigid_only']
    matrix=pose_matrix(check['original_fit']['pose'])
    assert np.linalg.det(matrix[:3,:3])==pytest.approx(1)
    assert np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3))
    assert [faces,scene]==before
    assert not check['source_drawing_error_confirmed'] and not check['engineering_verified']


def test_nonplanarity_is_reported_not_projected_away(fixture):
    faces,_,_,scene=copy.deepcopy(fixture)
    # Move one reference endpoint out of the plane, preserving the independent target.
    edge=next(e for f in faces if f['id']=='rear' for e in f['edges'] if e['mark']=='E')
    edge['b'][1]+=180
    report=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    assert report['prediction']['planarity_max_mm']>3
    assert any('不共面' in issue for issue in report['issues'])
    assert report['prediction']['raw_corners_world_mm']!=report['prediction']['planar_corners_world_mm']


def test_different_target_faces_get_distinct_evidence_urls(fixture):
    faces,_,_,scene=fixture
    a=infer_missing_face(faces,scene,'bridge1',[],'fixture')
    b=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    assert a['basis_signature']==b['basis_signature']
    assert a['artifact_key']!=b['artifact_key']


@pytest.mark.parametrize('kind',['repeated','unknown','different_source','disconnected'])
def test_ambiguous_or_unconnected_references_cannot_predict(fixture,kind):
    faces,_,_,scene=copy.deepcopy(fixture)
    for face in faces:
        if kind=='different_source':face['source_id']=face['id']
        for edge in face['edges']:
            if kind in {'repeated','unknown'}:edge['bom_quantity']=2 if kind=='repeated' else None
    if kind=='disconnected':scene['links']=[]
    assert not missing_face_candidates(faces,scene)
    with pytest.raises(ValueError,match='至少需要两个'):
        infer_missing_face(faces,scene,'bridge2',[],'fixture')


def native_fixture(two_sides=True):
    member={'axis':{'a':[100,0],'b':[500,0]},'polygon':[[100,-5],[500,-5],[500,5],[100,5]]}
    strokes=[{'id':'lower','a':[-50,-5],'b':[500,-5],'primitive_ids':['raw1']},
             {'id':'upper','a':[-50 if two_sides else 100,5],'b':[500,5],'primitive_ids':['raw2']},
             {'id':'unrelated','a':[-1000,-5],'b':[-700,-5],'primitive_ids':['raw3']}]
    return member,{'strokes':strokes}


def test_native_extension_uses_both_outlines_and_keeps_uncertainty():
    member,geometry=native_fixture()
    member['bom_length_mm']=3000  # Must not control extrapolation.
    report=native_axis_evidence(member,geometry,2,[0,-50,600,50])
    assert report['status']=='native_lines_support_missing_span'
    assert report['candidate_length_mm']==1100
    assert report['observed_length_mm']==800
    assert report['axis_pt']=={'a':[-50,0],'b':[500,0]}
    assert not report['full_endpoints_verified']
    assert {s['id'] for s in geometry['strokes']}=={'lower','upper','unrelated'}
    assert {s['stroke_id'] for s in report['strokes']}=={'lower','upper'}
    member['bom_length_mm']=5000
    assert native_axis_evidence(member,geometry,2,[0,-50,600,50])==report


def test_one_sided_line_cannot_confirm_missing_member():
    member,geometry=native_fixture(False)
    evidence=native_axis_evidence(member,geometry,2,[0,-50,600,50])
    assert evidence['status']=='no_independent_missing_span_confirmation'


def test_unconnected_faces_remain_in_options_with_reasons(fixture):
    faces,_,_,scene=copy.deepcopy(fixture)
    scene['links']=[]
    faces[-1]['units']='pdf_pt'
    options=missing_face_options(faces,scene)
    assert {o['face_id'] for o in options}=={f['id'] for f in faces}
    assert all(not o['ready'] and o['reason'] for o in options)
    assert any('比例未定' in o['reason'] for o in options)
    assert next(o for o in options if o['face_id']=='bridge2')['neighbor_count']==1  # The front reference alone is insufficient.


def test_review_preserves_layout_history_and_invalidates_on_pose_change(tmp_path,fixture,monkeypatch):
    faces,matches,parallel,scene=copy.deepcopy(fixture)
    projects=AssemblyProjects(SimpleNamespace(root=tmp_path,lock=RLock(),jobs={}))
    record={'id':'check','title':'check','revision':7,'scene':scene,'undo':[copy.deepcopy(scene)],'redo':[]}
    data={'id':'check','faces':faces,'parallel':parallel,'matches':matches,'waiting':False,'input_digest':'fixture'}
    projects.records['check']=record;projects.persist(record,data)
    monkeypatch.setattr(projects,'refresh',lambda aid:data)
    monkeypatch.setattr(projects,'source_data',lambda record:([],[],None))
    monkeypatch.setattr('tower_rebuild.assembly_projects.write_review_artifacts',lambda *args:None)
    before=copy.deepcopy(scene)
    reviewed=projects.mutate('check',{'action':'review_missing_face','face_id':'bridge2','revision':7})
    assert reviewed['scene']==before and reviewed['history']=={'undo':1,'redo':0}
    assert reviewed['missing_face_review']['target_face_id']=='bridge2'
    with pytest.raises(AssemblyConflict):
        projects.mutate('check',{'action':'review_missing_face','face_id':'bridge2','revision':7})
    pose=copy.deepcopy(scene['poses']['rear']);pose['position'][2]+=20
    updated=projects.mutate('check',{'action':'pose','revision':8,'face_id':'rear','pose':pose,'move_connected':False})
    assert updated['missing_face_review'] is None and updated['missing_face_review_stale']
    undone=projects.mutate('check',{'action':'undo','revision':9})
    assert undone['scene']==before and undone['missing_face_review'] is not None


def test_only_hash_verified_observations_are_read(tmp_path):
    jobs=SimpleNamespace(root=tmp_path,lock=RLock(),jobs={});add_source(jobs,'one')
    projects=AssemblyProjects(jobs);p=projects.create(['one']);record=projects.records[p['id']]
    top=tmp_path/'one/run/site/topology';write_json(top/'members.json',[{'id':'native'}])
    assert projects.source_data(record)[0][0]['member_observations']==[]
    model=read_json(top/'model-preview.json');model['input_files']['topology/members.json']=file_digest(top/'members.json')
    write_json(top/'model-preview.json',model)
    assert projects.source_data(record)[0][0]['member_observations']==[{'id':'native'}]
    write_json(top/'members.json',[{'id':'tampered'}])
    sources,summary,_=projects.source_data(record)
    assert not sources and summary[0]['error']
