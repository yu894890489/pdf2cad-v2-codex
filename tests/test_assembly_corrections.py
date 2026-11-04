import copy
import math
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from tower_rebuild.assembly_corrections import (split_length_change, build_face_correction,
    apply_face_correction, place_correction, validate_corrections)
from tower_rebuild.assembly_fitting import fit_tapered_faces
from tower_rebuild.assembly_missing_faces import infer_missing_face
from tower_rebuild.assembly_projects import AssemblyProjects
from tower_rebuild.io import read_json
from test_assembly_fitting import tapered_fixture


@pytest.fixture(scope='module')
def correction_fixture():
    faces,matches,parallel,scene=tapered_fixture()
    scene=fit_tapered_faces(faces,matches,parallel,scene,'negative_x')
    for face in faces:
        face['view_id']=face['id'];face['holes']=[]
        for edge in face['edges']:
            if face['id']=='bridge2':
                a,b=np.array(edge['a']),np.array(edge['b'])
                edge['a']=(a*.85+b*.15).tolist();edge['b']=(a*.15+b*.85).tolist()
                edge['length']=math.dist(edge['a'],edge['b'])
            face['members'].append(dict(id=edge['member_id'],mark=edge['mark'],a=edge['a'],b=edge['b'],
                bom_length_mm=edge['bom_length_mm'],section={'legs_mm':[75,75],'thickness_mm':6}))
    target=next(f for f in faces if f['id']=='bridge2')
    target['members'].append(dict(id='diagonal',mark='G',a=target['edges'][0]['a'],b=target['edges'][1]['b'],
        bom_length_mm=1200,section={'legs_mm':[50,50],'thickness_mm':5}))
    target['plates']=[{'polygon':[[0,0,0],[50,0,0],[0,50,0]],'stock':{'thickness_mm':8},
        'drill_holes_local':[{'center':[10,10,0],'diameter_mm':22}]}]
    target['holes']=[{'center':[10,10,0],'radius':11}]
    return faces,matches,parallel,scene


@pytest.mark.parametrize('original,center',[(60,50),(140,50),(100,-20),(100,140),(40,150),(40,-80)])
def test_length_segments_conserve_original_and_corrected_material(original,center):
    segments=split_length_change([0,0,0],[100,0,0],[center,0,0],original)
    lengths={kind:sum(s['length_mm'] for s in segments if s['kind']==kind) for kind in ('retained','added','trimmed')}
    assert lengths['retained']+lengths['added']==pytest.approx(100)
    assert lengths['retained']+lengths['trimmed']==pytest.approx(original)
    physical=sorted([s for s in segments if s['kind']!='trimmed'],key=lambda s:s['a'][0])
    assert physical[0]['a'][0]==pytest.approx(0) and physical[-1]['b'][0]==pytest.approx(100)
    for a,b in zip(physical,physical[1:]):assert a['b']==pytest.approx(b['a'])


def test_whole_face_correction_places_shared_axes_preserves_sources_and_sections(correction_fixture):
    faces,_,_,scene=copy.deepcopy(correction_fixture);before=copy.deepcopy([faces,scene])
    target=next(f for f in faces if f['id']=='bridge2')
    report=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    revised,corrected=place_correction(faces,scene,target,report,'fixture')
    validate_corrections(revised,[corrected if f['id']=='bridge2' else f for f in faces])
    assert [faces,scene]==before
    assert len(corrected['members'])==3
    assert report['correction']['max_endpoint_gap_mm']<.001
    assert report['correction']['extended_members']>=2
    for old,new in zip(target['members'],corrected['members']):
        assert new['section']==old['section'] and new['bom_length_mm']==old['bom_length_mm']
        assert new['correction']['original_axis_local']==[old['a'],old['b']]
        assert not new['endpoints_verified']
    assert corrected['holes'][0]['radius']==11
    assert corrected['plates'][0]['stock']=={'thickness_mm':8}
    assert corrected['plates'][0]['drill_holes_local'][0]['diameter_mm']==22
    assert not report['engineering_verified']
    for fid,pose in scene['poses'].items():
        if fid!='bridge2':assert revised['poses'][fid]==pose
    again=infer_missing_face(faces,revised,'bridge2',[],'fixture')
    repeated,_=place_correction(faces,revised,target,again,'fixture')
    assert repeated==revised


def test_nonplanar_neighbors_are_reported_without_forcing_plane(correction_fixture):
    faces,_,_,scene=copy.deepcopy(correction_fixture)
    edge=next(e for f in faces if f['id']=='rear' for e in f['edges'] if e['mark']=='E')
    edge['b'][1]+=180
    report=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    target=next(f for f in faces if f['id']=='bridge2')
    with pytest.raises(ValueError,match='离平面'):build_face_correction(target,report,'fixture')


def test_saved_corrections_undo_redo_restart_and_no_accumulation(tmp_path,monkeypatch,correction_fixture):
    faces,matches,parallel,scene=copy.deepcopy(correction_fixture)
    projects=AssemblyProjects(SimpleNamespace(root=tmp_path,lock=RLock(),jobs={}))
    record={'id':'check','title':'check','revision':1,'scene':scene,'undo':[],'redo':[]}
    data={'id':'check','faces':faces,'parallel':parallel,'matches':matches,'waiting':False,'input_digest':'fixture'}
    projects.records['check']=record;projects.persist(record,data)
    monkeypatch.setattr(projects,'refresh',lambda aid:data)
    monkeypatch.setattr(projects,'source_data',lambda record:([],[],None))
    monkeypatch.setattr('tower_rebuild.assembly_projects.write_review_artifacts',lambda *args:None)
    original=copy.deepcopy(data)
    def act(action,**args):return projects.mutate('check',{'action':action,'revision':record['revision'],**args})
    applied=act('review_missing_face',face_id='bridge2')
    assert applied['history']=={'undo':1,'redo':0}
    assert applied['geometry_revision']!=original['geometry_revision']
    repeated=act('review_missing_face',face_id='bridge2')
    assert repeated['faces']==applied['faces'] and repeated['history']==applied['history']
    reverted=act('undo')
    assert reverted['faces']==original['faces'] and reverted['scene']==original['scene']
    restored=act('redo')
    assert restored['faces']==applied['faces'] and restored['scene']==applied['scene']
    # Rehydrate a fresh repository using immutable extracted faces and saved scene.
    fresh=AssemblyProjects(projects.jobs);loaded=copy.deepcopy(original)
    fresh.persist(fresh.records['check'],loaded)
    assert loaded['faces']==applied['faces'] and loaded['geometry_revision']==applied['geometry_revision']
    assert read_json(tmp_path/'assemblies/check/project.json')['scene']==applied['scene']
    bad=copy.deepcopy(applied['scene']);bad['face_corrections']['bridge2']['source_face_digest']='different'
    with pytest.raises(ValueError,match='原始几何'):act('import',input_digest='fixture',scene=bad)
    assert record['scene']==applied['scene']
    arranged=act('arrange')
    assert arranged['faces']==applied['faces'] and not arranged['scene']['links']
    assert arranged['scene']['face_corrections']==applied['scene']['face_corrections']
    assert act('undo')['scene']==applied['scene']
    assert (tmp_path/'assemblies/check/missing-face-corrected.svg').is_file()


@pytest.mark.parametrize('damage',['matrix','member','segment','length','hash','status'])
def test_invalid_correction_import_rejected(correction_fixture,damage):
    faces,_,_,scene=copy.deepcopy(correction_fixture);target=next(f for f in faces if f['id']=='bridge2')
    report=infer_missing_face(faces,scene,'bridge2',[],'fixture')
    scene,_=place_correction(faces,scene,target,report,'fixture')
    c=scene['face_corrections']['bridge2']
    if damage=='matrix':c['mapping']=[[0]*3]*3
    if damage=='member':c['members']=[None]
    if damage=='segment':c['members'][0]['segments']=[None]
    if damage=='length':c['members'][0]['corrected_length_mm']+=100
    if damage=='hash':c.pop('source_face_digest')
    if damage=='status':c['summary']['engineering_verified']=True
    with pytest.raises(ValueError):validate_corrections(scene,faces)
