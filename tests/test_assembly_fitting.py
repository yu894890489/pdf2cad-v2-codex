import copy
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from tower_rebuild.assembly import (initial_scene, edge_matches, assess_scene,
                                    connect_faces, transformed)
from tower_rebuild.assembly_fitting import fit_tapered_faces
from tower_rebuild.assembly_projects import AssemblyProjects


def planar_face(fid, polygon, members, u, v, origin=(0, 0, 0)):
    """Independent 3D fixture projected onto orthonormal drawing coordinates."""
    u, v, origin = np.asarray(u, float), np.asarray(v, float), np.asarray(origin, float)
    u /= np.linalg.norm(u); v /= np.linalg.norm(v)
    def local(p):
        delta = np.asarray(p)-origin
        return [float(delta @ u), float(delta @ v), 0.]
    edges = []
    for mark, a, b in members:
        length = float(np.linalg.norm(np.asarray(a)-b))
        edges.append(dict(id=f'{fid}:{mark}', face_id=fid, member_id=mark, mark=mark,
                          a=local(a), b=local(b), length=length, specification='L75X6',
                          bom_length_mm=length, bom_quantity=1, boundary=False,
                          basis='observed_member_axis'))
    return dict(id=fid, source_id='one-drawing', title=fid, units='mm', side='whole', role='unassigned',
                polygon=[local(p) for p in polygon], edges=edges, members=[], plates=[], wire=[])


def tapered_fixture():
    f = [[0,0,0], [1000,0,0], [1000,600,0], [0,600,0]]
    r = [[x,y,-200-x*.3] for x,y,_ in f]
    fa, fb = [250,0,0], [250,600,0]
    ra, rb = [250,0,-275], [250,600,-275]
    front_members = [('A',f[0],f[1]), ('B',f[3],f[2]), ('C',fa,fb)]
    rear_members = [('D',r[0],r[1]), ('E',r[3],r[2]), ('F',ra,rb)]
    faces = [planar_face('front',f,front_members,[1,0,0],[0,1,0]),
             planar_face('rear',r,rear_members,[1,0,-.3],[0,1,0],r[0]),
             planar_face('bridge1',[f[0],f[1],r[1],r[0]], [front_members[0],rear_members[0]], [1,0,0],[0,0,-1]),
             planar_face('bridge2',[f[3],f[2],r[2],r[3]], [front_members[1],rear_members[1]], [1,0,0],[0,0,-1],f[3]),
             planar_face('bridge3',[fa,fb,rb,ra], [front_members[2],rear_members[2]], [0,1,0],[0,0,-1],fa),
             planar_face('unrelated',f,[],[1,0,0],[0,1,0])]
    parallel = [{'a':'front','b':'rear','basis':'explicit_front_rear_marks'}]
    matches = edge_matches(faces,parallel)
    scene = initial_scene(faces);scene['front_face_id']='front'
    # Reproduce a user's reversed endpoint layout; fitter must correct it.
    scene = connect_faces(scene,faces,'front:A','bridge1:A',90,True)
    scene = connect_faces(scene,faces,'bridge1:D','rear:D',90,True)
    return faces,matches,parallel,scene


@pytest.fixture(scope='module')
def fitted():
    faces,matches,parallel,scene=tapered_fixture()
    result=fit_tapered_faces(faces,matches,parallel,scene,'negative_x')
    return faces,matches,parallel,scene,result


def test_fit_recovers_taper_from_interior_shared_members_without_stretching(fitted):
    faces,matches,parallel,before,scene=fitted
    assert not any(m['automatic_candidate'] for m in matches)
    assert len(scene['links'])==6
    assert scene['poses']['front']==before['poses']['front']
    assert scene['poses']['unrelated']==before['poses']['unrelated']
    report=assess_scene(faces,scene,parallel)
    relation=report['parallel_relations'][0]
    assert relation['constraint_kind']=='taper' and relation['satisfied']
    assert relation['taper']['tip_width_mm']==pytest.approx(200,abs=.01)
    assert relation['taper']['root_width_mm']==pytest.approx(500,abs=.01)
    assert max(link['endpoint_gap_mm'] for link in report['links'])<.01
    assert not report['engineering_verified']
    for face in faces:
        for edge in face['edges']:
            a,b=transformed([edge['a'],edge['b']],scene['poses'][face['id']])
            assert np.linalg.norm(np.asarray(a)-b)==pytest.approx(edge['length'])


def test_repeated_unknown_bom_items_and_cross_document_marks_cannot_prove_placement():
    for kind in ['repeat','unknown','other_source']:
        faces,matches,parallel,scene=tapered_fixture()
        for face in faces:
            if kind=='other_source':face['source_id']=face['id']
            else:
                for edge in face['edges']:edge['bom_quantity']=4 if kind=='repeat' else None
        with pytest.raises(ValueError,match='至少需要两个'):
            fit_tapered_faces(faces,matches,parallel,scene,'negative_x')


def test_different_tip_direction_is_not_forced_with_a_worse_fit(fitted):
    faces,matches,parallel,before,_=fitted
    with pytest.raises(ValueError,match='尖端方向不一致'):
        fit_tapered_faces(faces,matches,parallel,before,'positive_x')


def test_taper_is_remeasured_and_missing_links_remain_unverified(fitted):
    faces,_,parallel,_,scene=fitted
    scene=copy.deepcopy(scene)
    scene['links']=[]
    assert not assess_scene(faces,scene,parallel)['parallel_relations'][0]['satisfied']
    # A live pose edit must invalidate the width calculation, not reuse fit output.
    scene['poses']['rear']=copy.deepcopy(scene['poses']['front'])
    relation=assess_scene(faces,scene,parallel)['parallel_relations'][0]
    assert not relation['taper']['narrowing']


def test_joint_counts_include_placed_faces_with_dimension_errors(tmp_path):
    from test_assembly import square
    faces=[square('A',size=1000),square('B',size=1006.06)]
    scene=connect_faces(initial_scene(faces),faces,'A:0','B:0')
    projects=AssemblyProjects(SimpleNamespace(root=tmp_path,lock=RLock(),jobs={}))
    record={'id':'partial','title':'partial','revision':1,'scene':scene,'undo':[]}
    data={'faces':faces,'parallel':[],'matches':[],'waiting':False}
    data=projects.persist(record,data)
    assert data['assessment']['located_face_count']==2
    assert data['assessment']['pending_connection_count']==1
    assert data['assessment']['valid_connection_count']==0
    assert data['assessment']['links'][0]['endpoint_gap_mm']==pytest.approx(3.03)
    assert not data['assessment']['watertight']


def test_preview_apply_undo_and_redo_preserve_the_original_layout(tmp_path, fitted, monkeypatch):
    faces,matches,parallel,before,proposed=fitted
    projects=AssemblyProjects(SimpleNamespace(root=tmp_path,lock=RLock(),jobs={}))
    record={'id':'fit','title':'fit','revision':7,'scene':copy.deepcopy(before),'undo':[],'redo':[]}
    data={'id':'fit','faces':faces,'parallel':parallel,'matches':matches,'waiting':False,'input_digest':'fixture'}
    projects.records['fit']=record;projects.persist(record,data)
    monkeypatch.setattr(projects,'refresh',lambda aid:data)
    # Numerical behavior is tested above; here exercise persistence independently.
    monkeypatch.setattr('tower_rebuild.assembly_projects.fit_tapered_faces',lambda *args:copy.deepcopy(proposed))
    saved=(projects.root/'fit/project.json').read_bytes()
    preview=projects.mutate('fit',{'action':'preview_taper','revision':7,'tip_direction':'negative_x'})
    assert preview['preview'] and preview['scene']==proposed
    assert (projects.root/'fit/project.json').read_bytes()==saved
    applied=projects.mutate('fit',{'action':'fit_taper','revision':7,'tip_direction':'negative_x'})
    assert applied['scene']==proposed and applied['history']=={'undo':1,'redo':0}
    undone=projects.mutate('fit',{'action':'undo','revision':8})
    assert undone['scene']==before
    redone=projects.mutate('fit',{'action':'redo','revision':9})
    assert redone['scene']==proposed


def test_auto_uses_remembered_tip_direction_after_arrange(tmp_path,fitted,monkeypatch):
    faces,matches,parallel,_,fitted_scene=copy.deepcopy(fitted)
    projects=AssemblyProjects(SimpleNamespace(root=tmp_path,lock=RLock(),jobs={}))
    scene=initial_scene(faces)
    record={'id':'auto','title':'auto','revision':1,'scene':scene,'undo':[fitted_scene],'redo':[]}
    data={'id':'auto','faces':faces,'parallel':parallel,'matches':matches,'waiting':False,'input_digest':'fixture'}
    projects.records['auto']=record;projects.persist(record,data)
    assert data['tip_direction_hint']=='negative_x'
    monkeypatch.setattr(projects,'refresh',lambda aid:data)
    calls=[]
    def fit(*args):calls.append(args[-1]);return copy.deepcopy(fitted_scene)
    monkeypatch.setattr('tower_rebuild.assembly_projects.fit_tapered_faces',fit)
    applied=projects.mutate('auto',{'action':'auto','revision':1})
    assert calls==['negative_x']
    assert applied['auto_attempt']['status']=='partial_candidate' and applied['auto_attempt']['changed']
    assert applied['scene']==fitted_scene and applied['history']['undo']==2
    again=projects.mutate('auto',{'action':'auto','revision':2})
    assert again['history']==applied['history'] and not again['auto_attempt']['changed']
    assert again['scene']==fitted_scene


def test_auto_requires_direction_without_previous_user_choice(fitted,monkeypatch):
    faces,matches,parallel,scene,_=fitted
    def should_not_fit(*args):raise AssertionError('Direction must not be guessed.')
    monkeypatch.setattr('tower_rebuild.assembly_projects.fit_tapered_faces',should_not_fit)
    data={'faces':faces,'matches':matches,'parallel':parallel}
    after,report=AssemblyProjects.attempt_auto(data,scene,None)
    assert after==scene and report['status']=='needs_tip_direction'
    assert '尖端方向' in report['message']


def test_auto_reports_partial_solver_failure_without_moving(fitted,monkeypatch):
    faces,matches,parallel,scene,_=fitted
    def failed(*args):raise ValueError('具体约束不满足。')
    monkeypatch.setattr('tower_rebuild.assembly_projects.fit_tapered_faces',failed)
    data={'faces':faces,'matches':matches,'parallel':parallel}
    after,report=AssemblyProjects.attempt_auto(data,scene,'negative_x')
    assert after==scene and '具体约束不满足' in report['message']
    assert not report['found']


def noisy_fixture(offset):
    faces, _, parallel, _ = tapered_fixture()
    # A displaced observation in one independently drawn bridge: stock
    # lengths and the corresponding observations in other faces stay intact.
    edge = next(e for f in faces if f['id'] == 'bridge2' for e in f['edges'] if e['mark'] == 'E')
    for point in (edge['a'], edge['b']):
        point[0] += offset
    scene = initial_scene(faces); scene['front_face_id'] = 'front'
    return faces, edge_matches(faces, parallel), parallel, scene


def test_noisy_views_share_residual_without_stretching_or_accepting_engineering(fitted):
    faces, matches, parallel, before = noisy_fixture(50)
    untouched = copy.deepcopy(faces)
    result = fit_tapered_faces(faces, matches, parallel, before, 'negative_x')
    evidence = result['taper_fit']['refinement']
    assert evidence['after_residual_norm_mm'] < evidence['before_residual_norm_mm']
    assert evidence['full_rank'] and not evidence['geometry_modified']
    assert result['poses']['front'] == before['poses']['front']
    assert result['poses']['unrelated'] == before['poses']['unrelated']
    assert faces == untouched
    assert all(row['within_candidate_limits'] for row in result['taper_fit']['seams'])
    assert not all(row['valid'] for row in result['taper_fit']['seams'])
    report = assess_scene(faces, result, parallel)
    assert not report['watertight'] and not report['engineering_verified']
    assert report['parallel_relations'][0]['taper']['narrowing']
    for face in faces:
        for edge in face['edges']:
            a, b = transformed([edge['a'], edge['b']], result['poses'][face['id']])
            assert np.linalg.norm(np.asarray(a)-b) == pytest.approx(edge['length'])


def test_large_inconsistency_is_rejected_with_member_diagnostics():
    from tower_rebuild.assembly_fitting import AssemblyFitError
    faces, matches, parallel, scene = noisy_fixture(500)
    original = copy.deepcopy((faces, scene))
    with pytest.raises(AssemblyFitError, match='件号') as caught:
        fit_tapered_faces(faces, matches, parallel, scene, 'negative_x')
    assert any(not row['within_candidate_limits'] for row in caught.value.diagnostics)
    assert all(row['face_a'] and row['face_b'] for row in caught.value.diagnostics)
    assert (faces, scene) == original


def test_joint_refinement_cannot_fix_an_underconstrained_single_bridge(fitted):
    from tower_rebuild.assembly_fitting import _refine_rigid_poses, _bridges
    faces, matches, parallel, before, scene = fitted
    connections = _bridges(faces, matches, 'front', 'rear')[:1]
    ids = {e['face_id'] for row in connections for e in row}
    poses = {fid: copy.deepcopy(scene['poses'][fid]) for fid in ids}
    poses['rear']['position'][0] += 5
    assert _refine_rigid_poses(connections, 'front', poses) is None
