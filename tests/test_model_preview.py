import copy
import pytest

from tower_rebuild.materials import classify
from tower_rebuild.model_preview import snap_native_junctions, plate_outline, view_scale, explicit_fan_holes


def stroke(a,b,name='ink'):
    return {'a':a,'b':b,'primitive_ids':[name]}


def test_stock_geometry_uses_hyphen_for_plate_and_l_for_angle():
    assert classify('Q345L70×5')['legs_mm']==[70,70]
    assert classify('Q345-8×394')['kind']=='plate'
    assert classify('Q345-8×394')['thickness_mm']==8
    assert classify('Q345-8×394')['width_mm']==394
    assert classify('Q420L125X10')['kind']=='angle'
    assert classify('-16X60')['thickness_mm']==16
    assert classify('Q345')['kind']=='unknown'


def test_only_small_native_junction_gaps_are_closed_and_input_is_preserved():
    strokes=[stroke([0,0],[100,0]),stroke([50,.4],[50,50],'near'),stroke([70,3],[70,50],'far')]
    before=copy.deepcopy(strokes);updated,changes=snap_native_junctions(strokes)
    assert updated[1]['a']==[50,0]
    assert updated[2]['a']==[70,3]
    assert strokes==before
    assert all(c['distance_pt']<=.7 for c in changes)


def plate_fixture():
    plate={'bom':{'specification':'Q345-6X20','length_mm':40},'callout_bbox_pt':[-4,-4,-2,-2]}
    contour={'id':'native','polygon':[[0,0],[20,0],[20,10],[0,10],[0,0]],'hole_ids':['h'],'primitive_ids':['a','b','c','d']}
    return plate,contour


def test_plate_stock_dimensions_do_not_create_a_rectangle_without_native_contour():
    p,c=plate_fixture()
    assert plate_outline(p,[],2)==(None,[])
    chosen,_=plate_outline(p,[c],2)
    assert chosen['polygon_pt']==c['polygon'] and not chosen['engineering_verified']
    p['bom']['length_mm']=400
    assert plate_outline(p,[c],2)==(None,[])


def test_multiple_equally_plausible_plate_boundaries_remain_pending():
    p,c=plate_fixture();other=copy.deepcopy(c);other['id']='another'
    other['polygon']=[[x,y+2] for x,y in c['polygon']]
    chosen,options=plate_outline(p,[c,other],2)
    assert chosen is None and len(options)==2


def test_plate_regions_without_hole_support_are_not_lattice_gussets():
    p,c=plate_fixture();c['hole_ids']=[]
    assert plate_outline(p,[c],2)==(None,[])


def test_explicit_dimensions_take_priority_over_bom_fitted_scale():
    polygons=[{'view_id':'v','closure':{'status':'consistent_with_rounding'},'edges':[
        {'text_id':'label','value_mm':100,'structural_a_pt':[0,0],'structural_b_pt':[20,0]}]}]
    member={'id':'m','overlap_recovery':{'scale_evidence':{'mm_per_pt_candidate':99}}}
    r=view_scale({'id':'v','role':'structural_view'},polygons,[member],[],[])
    assert r['mm_per_pt']==5 and r['basis']=='dimension_span_projection_candidate' and not r['verified']
    assert view_scale({'id':'v','role':'structural_view'},[],[{'overlap_recovery':{'scale_evidence':None}}],[],[])['mm_per_pt'] is None


def fan_fixture():
    view={'diameter_annotations':[{'primitive_id':'label','count':3,'diameter_mm':20,'bbox_pt':[-10,-5,0,5]}]}
    holes=[{'id':str(i),'center_pt':[40,y],'diameter_mm':None,'diameter_evidence':[]} for i,y in enumerate([-30,0,30])]
    strokes=[stroke([5,0],h['center_pt'],h['id']) for h in holes]
    return view,holes,strokes


def test_count_and_shared_native_leaders_support_section_hole_diameter():
    v,h,s=fan_fixture();result=explicit_fan_holes(v,h,s)
    assert all(p['diameter_mm']==20 and 'label' in p['diameter_evidence'] for p in result)
    assert all(p['diameter_mm'] is None for p in h)


def test_hole_count_or_missing_leader_does_not_propagate_diameter():
    v,h,s=fan_fixture();assert all(p['diameter_mm'] is None for p in explicit_fan_holes(v,h,s[:2]))
    v['diameter_annotations'][0]['count']=4
    assert all(p['diameter_mm'] is None for p in explicit_fan_holes(v,h,s))


def test_a_transverse_cut_does_not_set_the_section_scale():
    polygons=[{'view_id':'other','closure':{'status':'consistent_with_rounding'},'edges':[
        {'text_id':'label','value_mm':700,'structural_a_pt':[0,0],'structural_b_pt':[100,0]}]}]
    members=[{'id':'cut','axis':{'a':[0,0],'b':[50,0]},'bom':{'length_mm':8000}},
             {'id':'longitudinal','axis':{'a':[0,0],'b':[0,200]},'bom':{'length_mm':1400}}]
    r=view_scale({'id':'section','role':'section_view'},polygons,members,[],[])
    assert r['mm_per_pt']==7 and r['evidence']==[{'ratio':7.,'id':'longitudinal'}]


def test_conflicting_projection_scales_stay_unresolved_instead_of_averaging():
    holes=[{'id':'a','diameter_mm':20,'radii_pt':[1,1]},
           {'id':'b','diameter_mm':20,'radii_pt':[10,10]}]
    result=view_scale({'id':'v','role':'detail'},[],[],[],holes)
    assert result['mm_per_pt'] is None
    assert result['basis']=='conflicting_projection_scales'
    assert len(result['evidence'])==2


def test_section_scale_crosscheck_does_not_borrow_from_another_page():
    polygons=[{'view_id':'other','page':2,'closure':{'status':'consistent_with_rounding'},'edges':[
        {'text_id':'label','value_mm':700,'structural_a_pt':[0,0],'structural_b_pt':[100,0]}]}]
    members=[{'id':'m','axis':{'a':[0,0],'b':[0,200]},'bom':{'length_mm':1400}}]
    assert view_scale({'id':'section','page':1,'role':'section_view'},polygons,members,[],[])['mm_per_pt'] is None
