import copy
import math

import pytest

from tower_rebuild.assembly import edge_matches, initial_scene
from tower_rebuild.assembly_review import review_assembly
from test_assembly import square


def test_shared_member_inside_envelope_is_available_for_location_only():
    a,b=square('A',['326',None,None,None]),square('B',['326',None,None,None])
    a['edges'][0]['boundary']=False
    matches=edge_matches([a,b],[])
    shared=[m for m in matches if m.get('shared_member_candidate')]
    assert len(shared)==1 and not shared[0]['automatic_candidate']
    assert not shared[0]['both_on_envelope']


def test_same_wrong_observed_lengths_do_not_become_automatic_evidence():
    a,b=square('A',['1',None,None,None]),square('B',['1',None,None,None])
    for f in (a,b):f['edges'][0]['bom_length_mm']=800
    same=next(m for m in edge_matches([a,b],[]) if m['kind']=='mark_spec_length')
    assert not same['automatic_candidate'] and not same['shared_member_candidate']


def bridge_fixture(tilt):
    a,b,c=square('A',['326',None,None,None]),square('B',['327',None,None,None]),square('C',['326',None,'327',None])
    edge=c['edges'][2]
    edge['a']=[1000.,1000.,0.];edge['b']=[1000.-1000*math.cos(math.radians(tilt)),1000+1000*math.sin(math.radians(tilt)),0.]
    faces=[a,b,c];parallel=[{'a':'A','b':'B'}]
    return review_assembly(faces,edge_matches(faces,parallel),parallel,initial_scene(faces))


def test_tapered_section_conflicts_with_parallel_front_rear_planes():
    report=bridge_fixture(20)
    pair=report['parallel_bridges'][0]
    assert pair['axis_angle_deg']==pytest.approx(20)
    assert not pair['compatible_with_parallel_faces']
    assert any(b['code']=='parallel_bridge_conflict' for b in report['blockers'])
    assert set(report['front_component'])=={'A','B','C'}


def test_reversed_parallel_axes_do_not_create_false_angle_conflict():
    pair=bridge_fixture(0)['parallel_bridges'][0]
    assert pair['compatible_with_parallel_faces']


def test_review_separates_projection_overlap_from_confirmed_misidentification():
    face=square('A',['363','364',None,None])
    face['edges'][1].update(a=face['edges'][0]['a'][:],b=face['edges'][0]['b'][:],bom_length_mm=1100)
    face['scale']={'basis':'bom_member_consensus_preview_only'}
    report=review_assembly([face],[],[],initial_scene([face]))
    assert report['length_conflicts'][0]['mark']=='364'
    assert report['overlapping_axes'][0]['status']=='check_projection_or_axis_assignment'
    assert 'scale_from_bom_preview' in report['face_reports'][0]['reasons']


def test_same_mark_spec_conflict_is_reported_and_not_joined():
    a,b=square('A',['1',None,None,None]),square('B',['1',None,None,None])
    b['edges'][0]['specification']='Q345L90X8'
    matches=edge_matches([a,b],[])
    assert not any(m['shared_member_candidate'] for m in matches)
    report=review_assembly([a,b],matches,[],initial_scene([a,b]))
    assert report['same_mark_conflicts'][0]['reasons']==['specification_conflict']
