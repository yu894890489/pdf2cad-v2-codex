import copy
import math

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from tower_rebuild.assembly import (initial_scene, edge_matches, auto_assemble,
    assess_scene, attached_pose, link_residual, connect_faces, transformed)


def square(fid, marks=None, size=1000):
    p = [[0.,0.,0.],[size,0.,0.],[size,size,0.],[0.,size,0.]]
    edges = [dict(id=f'{fid}:{i}', face_id=fid, a=a, b=b, length=float(size),
                  mark=(marks or [None]*4)[i], specification='Q345L75X6',
                  bom_length_mm=size, boundary=True, basis='observed_member_axis')
             for i,(a,b) in enumerate(zip(p,p[1:]+p[:1]))]
    return dict(id=fid, polygon=p, edges=edges, members=[], wire=[], units='mm',
                role='front' if fid=='A' else 'unassigned', side='whole')


def ring_faces():
    return [square('A',['ab',None,'da',None]), square('B',['bc',None,'ab',None]),
            square('C',['cd',None,'bc',None]), square('D',['da',None,'cd',None])]


def test_auto_ring_closes_without_claiming_end_caps():
    faces=ring_faces()
    result=auto_assemble(faces,edge_matches(faces,[]),[],'A')
    assert result['found']
    report=assess_scene(faces,result['scene'])
    assert report['status']=='lateral_ring_candidate'
    assert not report['watertight']
    assert len(report['open_edges'])==8
    assert all(r['valid'] for r in report['links'])
    normal=Rotation.from_quat(result['scene']['poses']['A']['quaternion']).apply([0,0,1])
    assert abs(normal[2])<1e-6  # front plane is vertical


def test_closed_cube_and_missing_face_and_gap():
    faces=[square(str(i)) for i in range(6)]
    scene=initial_scene(faces)
    # bottom/top, front/back, left/right
    angles=[(0,0,0),(0,0,0),(90,0,0),(90,0,0),(0,90,0),(0,90,0)]
    positions=[[0,0,0],[0,0,1000],[0,0,0],[0,1000,0],[0,0,1000],[1000,0,1000]]
    for f,angle,pos in zip(faces,angles,positions):
        scene['poses'][f['id']]={'position':pos,'quaternion':Rotation.from_euler('xyz',angle,degrees=True).as_quat().tolist()}
    report=assess_scene(faces,scene)
    assert report['watertight']
    assert report['euler_characteristic']==2
    assert report['volume_mm3']==pytest.approx(1e9)
    scene['active_face_ids'].remove('1')
    assert not assess_scene(faces,scene)['watertight']
    scene['active_face_ids'].append('1')
    scene['poses']['1']['position'][2]+=20
    assert not assess_scene(faces,scene)['watertight']


def test_no_automatic_assembly_from_equal_lengths_or_flat_overlap():
    faces=[square(str(i)) for i in range(4)]
    matches=edge_matches(faces,[])
    assert matches and not any(m['automatic_candidate'] for m in matches)
    assert not auto_assemble(faces,matches,[])['found']
    scene=initial_scene(faces)
    for fid in scene['poses']:scene['poses'][fid]={'position':[0,0,0],'quaternion':[0,0,0,1]}
    report=assess_scene(faces,scene)
    assert not report['watertight'] and not report['lateral_cycles']
    assert report['intersections']


@pytest.mark.parametrize('angle',[-135,-90,0,45,90,180])
@pytest.mark.parametrize('flip',[False,True])
def test_rigid_edge_attach_angles_and_direction(angle,flip):
    a,b=square('A'),square('B')
    scene=initial_scene([a,b]);old=copy.deepcopy(scene)
    scene=connect_faces(scene,[a,b],'A:0','B:2',angle,flip)
    edges={e['id']:e for f in (a,b) for e in f['edges']}
    residual=link_residual(scene['links'][0],edges,scene['poses'])
    assert residual['valid']
    assert scene['poses']['A']==old['poses']['A']
    pts=transformed(b['polygon'],scene['poses']['B'])
    assert math.dist(pts[0],pts[2])==pytest.approx(math.sqrt(2)*1000)


def test_length_conflict_does_not_stretch_and_unscaled_is_blocked():
    a,b=square('A'),square('B',size=1100)
    scene=connect_faces(initial_scene([a,b]),[a,b],'A:0','B:0')
    result=assess_scene([a,b],scene)
    assert result['links'][0]['endpoint_gap_mm']==pytest.approx(50)
    assert not result['links'][0]['valid']
    b['units']='pdf_pt'
    with pytest.raises(ValueError,match='mm/pt'):connect_faces(initial_scene([a,b]),[a,b],'A:0','B:0')


def test_front_rear_cannot_be_adjacent_auto_candidates():
    a,b=square('A',['1']*4),square('B',['1']*4)
    assert not edge_matches([a,b],[{'a':'A','b':'B'}])


def test_auto_adds_end_caps_only_with_multiple_shared_edges():
    faces=[square(str(i)) for i in range(6)]
    angles=[(0,0,0),(0,0,0),(90,0,0),(90,0,0),(0,90,0),(0,90,0)]
    positions=[[0,0,0],[0,0,1000],[0,0,0],[0,1000,0],[0,0,1000],[1000,0,1000]]
    keys={}
    for f,angle,pos in zip(faces,angles,positions):
        pose={'position':pos,'quaternion':Rotation.from_euler('xyz',angle,degrees=True).as_quat().tolist()}
        for edge in f['edges']:
            key=tuple(sorted(tuple(round(v) for v in p) for p in transformed([edge['a'],edge['b']],pose)))
            if key not in keys:keys[key]=str(len(keys)+1)
            edge['mark']=keys[key]
    result=auto_assemble(faces,edge_matches(faces,[]),[],'0')
    assert result['found']
    report=assess_scene(faces,result['scene'])
    assert report['watertight'] and report['active_faces']==6
    assert report['volume_mm3']==pytest.approx(1e9)


def test_existing_component_moves_rigidly():
    faces=[square(c) for c in 'ABC'];scene=initial_scene(faces)
    scene=connect_faces(scene,faces,'B:0','C:2')
    before=copy.deepcopy(scene)
    scene=connect_faces(scene,faces,'A:0','B:2')
    edges={e['id']:e for f in faces for e in f['edges']}
    assert all(link_residual(link,edges,scene['poses'])['valid'] for link in scene['links'])
    assert scene['poses']['C']!=before['poses']['C']


def test_parallel_front_rear_must_be_distinct_planes():
    a,b=square('A'),square('B')
    scene=initial_scene([a,b])
    report=assess_scene([a,b],scene,[{'a':'A','b':'B'}])
    assert report['parallel_relations'][0]['parallel_error_deg']==pytest.approx(0)
    assert not report['parallel_relations'][0]['satisfied']
    scene['poses']['B']['position'][1]=500
    assert assess_scene([a,b],scene,[{'a':'A','b':'B'}])['parallel_relations'][0]['satisfied']


def test_wrong_front_rear_pair_prevents_automatic_ring():
    faces=ring_faces();parallel=[{'a':'A','b':'B'}]
    assert not auto_assemble(faces,edge_matches(faces,parallel),parallel,'A')['found']


def test_chosen_front_is_not_silently_replaced():
    faces=ring_faces()+[square('unrelated')]
    assert not auto_assemble(faces,edge_matches(faces,[]),[],'unrelated')['found']
