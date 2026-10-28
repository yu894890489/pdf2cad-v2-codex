import copy
import numpy as np
import pytest

from tower_rebuild.dimension_audit import (
    adjacent_span_extensions, audit_dimensions, cosine_triangle, cosine_span_interval,
    discover_polygons, end_allowances, inline_annotations,
    span_bom_checks, straight_triangle_check, vector_check, compare_dimension_hypotheses,
    projected_axes, associate_polygon, summarize_views,
)
from tower_rebuild.io import write_json


def test_non_right_triangle_is_not_forced_to_pythagoras():
    check=cosine_triangle([4,5,6])
    assert check['status']=='valid_triangle'
    assert sum(check['angles_opposite_edges_deg'])==pytest.approx(180)
    assert all(abs(a-90)>5 for a in check['angles_opposite_edges_deg'])
    assert check['right_angle_assumed'] is False
    assert cosine_triangle([2,3,6])['status']=='triangle_inequality_failed'


def test_section11_single_bad_value_is_localized_by_independent_directions():
    directions=[[.72246989403725,.691402380824513],[-.722469804602326,.691402474278099],[0,-1]]
    check=vector_check([987,994,1375],directions,[.5]*3)
    assert check['residual_norm_mm']==pytest.approx(7.34869155)
    assert check['recommendation']['rounded_candidate_mm']==994
    assert check['recommendation']['edge_index']==0
    assert check['recommendation']['residual_after_mm']<.5
    good=vector_check([994,994,1375],directions,[.5]*3)
    assert good['status']=='consistent_with_rounding'
    assert good['recommendation'] is None


def test_nonunique_correction_does_not_choose_a_winner():
    check=vector_check([10,11,10,10],[[1,0],[0,1],[-1,0],[0,-1]],[.1]*4)
    assert check['status']=='consistent_with_rounding'  # conservative minimum 1 mm
    check=vector_check([10,13,10,10],[[1,0],[0,1],[-1,0],[0,-1]],[.1]*4)
    assert check['status']=='directional_inconsistency'
    assert check['recommendation'] is None  # either vertical side can explain it


def text(tid,value,xy,direction=(1,0),block=1,line=0):
    x,y=xy
    return {'id':tid,'text':str(value),'bbox_pt':[x-1,y-1,x+1,y+1],
            'direction':direction,'font_size_pt':5,'text_block':block,'text_line':line,
            'primitive_type':'text','part':'structure','page':1}


def span_scene():
    edge={'a':[0,0],'b':[10,0],'structural_a_pt':[0,0],'structural_b_pt':[100,0],
          'direction':[1,0],'axis_id':'axis','node_ids':['A','B'],'member_ids':['m'],
          'value_mm':100.,'text_id':'span','rounding_mm':.5}
    right={'a':[10,0],'b':[5,5],'node_ids':['B','C'],'value_mm':50.,'text_id':'right'}
    left={'a':[5,5],'b':[0,0],'node_ids':['C','A'],'value_mm':50.,'text_id':'left'}
    base={'id':'base','view_id':'v','association_status':'projected_face_candidate','edges':[edge,right,left]}
    other={'id':'extension','view_id':'v','association_status':'unresolved','bbox_pt':[5,0,12,5],'edges':[
        {'a':[5,5],'b':[10,0],'value_mm':50.,'text_id':'shared'},
        {'a':[10,0],'b':[12,0],'value_mm':20.,'rounding_mm':.5,'text_id':'extra'},
        {'a':[12,0],'b':[5,5],'value_mm':80.,'text_id':'outer'},
    ]}
    return edge,base,other


def test_explicit_end_offsets_required_and_ambiguous_labels_stay_pending():
    edge,_,_=span_scene()
    labels=[text('s','-5',(1,-4)),text('e','-5',(99,-4))]
    assert end_allowances(edge,labels)['status']=='two_end_label_candidates'
    assert not end_allowances(edge,labels[:1])['selected']
    assert not end_allowances(edge,labels+[text('other','-10',(98,-5))])['selected']
    assert not end_allowances(edge,[text('unsigned','5',(1,-4)),labels[1]])['selected']
    assert not end_allowances(edge,[text('perpendicular','-5',(1,-4),(0,1)),labels[1]])['selected']


def test_shared_side_extension_uses_geometry_and_labels_not_bom():
    edge,base,other=span_scene()
    check=adjacent_span_extensions(edge,base,[base,other])
    assert [e['value_mm'] for e in check['segments']]==[20]
    assert check['segments'][0]['endpoint']==1
    other['edges'][0]['value_mm']=51
    assert not adjacent_span_extensions(edge,base,[base,other])['segments']


def test_repeated_extension_evidence_not_added_twice_and_conflicting_lengths_pending():
    edge,base,other=span_scene();duplicate=copy.deepcopy(other);duplicate['id']='duplicate'
    result=adjacent_span_extensions(edge,base,[base,other,duplicate])
    assert len(result['segments'])==1
    duplicate['edges'][1]['value_mm']=21
    assert adjacent_span_extensions(edge,base,[base,other,duplicate])['ambiguous']


def test_span_plus_extension_minus_ends_does_not_rewrite_bom():
    edge,base,other=span_scene()
    # Only the relevant edge enters this fixture's span/BOM correspondence.
    for e in base['edges'][1:]:e.update(member_ids=[],axis_id='unused',direction=[0,1])
    member={'id':'m','mark':'7','bom':{'length_mm':110}}
    original=copy.deepcopy(member)
    records=span_bom_checks([base,other],{'v':[text('s','-5',(1,-4)),text('e','-5',(99,-4))]},[member])
    assert records[0]['computed_cut_length_mm']==110
    assert records[0]['formula']=='100 +20 -5 -5 = 110 mm'
    assert records[0]['status']=='consistent_given_end_basis'
    assert member==original
    member['bom']['length_mm']=999
    record=span_bom_checks([base,other],{'v':[text('s','-5',(1,-4)),text('e','-5',(99,-4))]},[member])[0]
    assert record['computed_cut_length_mm']==110
    assert record['status']=='end_basis_or_length_conflict'


def test_global_detection_preserves_sketch_with_unassigned_edge_and_text():
    coords=[[0,0],[30,0],[30,40]];primitives=[]
    for i in range(3):
        primitives.append({'id':f'l{i}','page':1,'part':'structure','role':'unassigned_geometry','primitive_type':'line',
                           'geometry':{'start':coords[i],'end':coords[(i+1)%3]},'style':{'width':.5},'line_style':'solid'})
    texts=[text('t0',300,(15,-4)),text('t1',400,(34,20),(0,1)),text('t2',500,(10,23),(.6,.8))]
    views=[{'id':'v','page':1,'bbox_pt':[-10,-10,40,50],'primitive_ids':['l0','l1','t0','t1']}]
    found=discover_polygons(primitives,{t['id']:t for t in texts},views,[],set())
    assert len(found)==1 and len(found[0]['edges'])==3
    assert found[0]['view_id']=='v'
    assert set(found[0]['previously_unassigned_primitive_ids'])=={'l2','t2'}
    primitives[2]['line_style']='dashed'
    assert not discover_polygons(primitives,{t['id']:t for t in texts},views,[],set())


def test_inline_circle_association_and_grade_prefix_do_not_corrupt_angle_section():
    texts=[text('spec','Q345L70X5',(0,0)),text('length',2306,(0,8),line=1)]
    row={'mark':'463','length_mm':2306,'specification':'Q345L70X5','classification':{'normalized':'Q345L70X5'}}
    member={'id':'m','mark':'463','view_id':'v','callout_id':'circle','callout_bbox_pt':[-3,-15,3,-9],'bom':row}
    axes=[{'id':'a','member_ids':['m'],'u':[1,0]}]
    result=inline_annotations(texts,axes,[member],[row],set())
    assert result[0]['status']=='consistent' and result[0]['matching_bom_marks']==['463']
    # An outline running in another direction is exposed, never hidden by a BOM match.
    axes[0]['u']=[0,1]
    assert inline_annotations(texts,axes,[member],[row],set())[0]['outline_direction_agrees'] is False


def test_stale_source_is_rejected_before_computing(tmp_path):
    source=tmp_path/'source.pdf';source.write_bytes(b'changed')
    write_json(tmp_path/'manifest.json',{'source':str(source),'source_sha256':'old'})
    with pytest.raises(ValueError,match='Source PDF changed'):audit_dimensions(tmp_path)


def test_adjacent_triangles_cosine_equation_without_right_angle():
    # A--O--D is a straight brace, B is the shared off-axis vertex.
    A=np.array([-987.,0]);O=np.array([0.,0]);B=np.array([0.,994]);D=np.array([1362.,0])
    def edge(a,b,nodes,axis,value,tid):
        d=b-a;return {'axis_id':axis,'node_ids':nodes,'direction':(d/np.linalg.norm(d)).tolist(),
                     'structural_a_pt':a.tolist(),'structural_b_pt':b.tolist(),'value_mm':value,'rounding_mm':.5,'text_id':tid}
    first={'id':'a','edges':[edge(O,B,['O','B'],'shared',994,'b'),edge(B,A,['B','A'],'outer1',1375,'s'),edge(A,O,['A','O'],'brace',987,'x')]}
    second={'id':'b','edges':[edge(B,O,['B','O'],'shared',994,'b2'),edge(O,D,['O','D'],'brace',1362,'y'),edge(D,B,['D','B'],'outer2',1721,'t')]}
    check=straight_triangle_check(first,second)[0]
    assert check['computed_mm']==pytest.approx(994.64469741)
    assert check['rounding_interval_mm'][0]<994<check['rounding_interval_mm'][1]
    # Collinear rays pointing to the same side do not define adjacent triangles.
    second['edges'][1]['direction']=[-1,0]
    assert not straight_triangle_check(first,second)


def test_cosine_interval_contains_perturbed_inputs_and_refuses_two_root_case():
    values=np.array([994.,1362.,1375.,1721.]);radii=np.array([.5]*4)
    _,(lo,hi)=cosine_span_interval(values,radii)
    for variation in np.random.default_rng(2026).uniform(-.5,.5,(200,4)):
        nominal,_=cosine_span_interval(values+variation,[0.]*4)
        assert lo<=nominal<=hi
    assert cosine_span_interval([10.,12.,9.,20.],[.01]*4) is None


def section11_decision_inputs():
    directions=[[.72246989403725,.691402380824513],[-.722469804602326,.691402474278099],[0,-1]]
    polygon={'id':'p','edges':[{'text_id':f't{i}','value_mm':v,'rounding_mm':.5} for i,v in enumerate([987,994,1375])],
             'closure':vector_check([987,994,1375],directions,[.5]*3)}
    spans=[{'id':'s','status':'consistent_given_end_basis','span_text_id':'t2','span_extensions':{'segments':[]},
            'computed_cut_length_mm':1285.,'bom_length_mm':1285.,'rounding_bound_mm':2.,'mark':'465',
            'formula':'1375 -45 -45 = 1285 mm','end_basis':{'selected':[{'text_id':'offset1'},{'text_id':'offset2'}]}}]
    adjacent=[{'polygon_ids':['p','neighbor'],'target_text_id':'t0','observed_mm':987.,'target_rounding_mm':.5,
               'input_values_mm':{'shared_side':994.,'opposite_segment':1362.,'first_outer_side':1375.,'second_outer_side':1721.},
               'input_text_ids':['t1','other1','t2','other2'],'input_rounding_mm':[.5]*4}]
    return polygon,spans,adjacent


def test_hypotheses_recompute_changed_inputs_and_compare_bom_without_mutation():
    polygon,spans,adjacent=section11_decision_inputs();before=copy.deepcopy((polygon,spans,adjacent))
    result=compare_dimension_hypotheses(polygon,spans,adjacent)
    assert result['status']=='cross_checked_candidate' and result['preferred']['candidate_mm']==994
    assert result['preferred']['bom_checks'][0]['computed_cut_length_mm']==1285
    alternative=next(h for h in result['hypotheses'] if h['candidate_mm']==1370)
    assert alternative['triangle_checks'][0]['computed_mm']==pytest.approx(987.401236,abs=.001)
    assert alternative['bom_checks'][0]['computed_cut_length_mm']==1280
    assert alternative['bom_checks'][0]['passed'] is False
    assert alternative['all_available_checks_passed'] is False
    assert (polygon,spans,adjacent)==before


def test_contradictory_neighbor_disqualifies_direction_only_recommendation():
    polygon,spans,adjacent=section11_decision_inputs()
    adjacent[0]['input_values_mm']['second_outer_side']=1750.
    assert polygon['closure']['recommendation']['rounded_candidate_mm']==994
    result=compare_dimension_hypotheses(polygon,spans,adjacent)
    assert result['preferred'] is None and result['status']=='underdetermined'


def test_direction_only_candidate_is_labelled_conditional():
    polygon,_,_=section11_decision_inputs()
    result=compare_dimension_hypotheses(polygon,[],[])
    assert result['preferred']['candidate_mm']==994
    assert result['status']=='direction_only_candidate'


def test_bom_anchor_can_disambiguate_two_direction_solutions():
    polygon={'id':'p','edges':[{'text_id':f't{i}','value_mm':v,'rounding_mm':.1} for i,v in enumerate([10,13,10,10])],
             'closure':vector_check([10,13,10,10],[[1,0],[0,1],[-1,0],[0,-1]],[.1]*4)}
    assert compare_dimension_hypotheses(polygon,[],[])['preferred'] is None
    spans=[{'id':'s','status':'consistent_given_end_basis','span_text_id':'t1','span_extensions':{'segments':[]},
            'computed_cut_length_mm':13.,'bom_length_mm':13.,'rounding_bound_mm':.2,'mark':'test',
            'formula':'13 +0 +0 = 13 mm','end_basis':{'selected':[{'text_id':'a'},{'text_id':'b'}]}}]
    result=compare_dimension_hypotheses(polygon,spans,[])
    assert result['preferred']['changed_text_id']=='t3' and result['preferred']['candidate_mm']==13


def test_nonunique_cosine_solution_is_not_counted_as_support():
    polygon,spans,adjacent=section11_decision_inputs()
    adjacent[0]['input_values_mm']['first_outer_side']=900.
    result=compare_dimension_hypotheses(polygon,spans,adjacent)
    candidate=next(h for h in result['hypotheses'] if h['candidate_mm']==994)
    assert candidate['triangle_checks'][0]['passed'] is None
    assert result['preferred'] is None


def test_small_direction_residual_cannot_validate_degenerate_triangle():
    polygon={'id':'p','edges':[{'text_id':f't{i}','value_mm':v,'rounding_mm':.5} for i,v in enumerate([2,3,6])],
             'closure':{'residual_norm_mm':4.,'rounding_bound_mm':1.5,'single_value_alternatives':[
                 {'edge_index':2,'rounded_candidate_mm':5.,'residual_after_mm':.01}]}}
    result=compare_dimension_hypotheses(polygon,[],[])
    assert result['hypotheses'][1]['direction_passed'] is True
    assert result['hypotheses'][1]['side_lengths_valid'] is False
    assert result['preferred'] is None


def face_scene():
    vertices=[[30,20],[50,20],[40,40]]
    poly={'id':'p','vertices_pt':vertices,'edges':[{'a':vertices[i],'b':vertices[(i+1)%3]} for i in range(3)]}
    view={'id':'v','bbox_pt':[-10,-20,410,110]}
    members=[{'id':name,'mark':name,'view_id':'v','axis':{'a':a,'b':b}}
             for name,a,b in [('base',[0,0],[100,0]),('far',[300,-10],[400,-10]),
                              ('right',[100,0],[50,100]),('left',[50,100],[0,0])]]
    return poly,view,members


def test_infinite_far_line_not_used_when_only_one_face_has_native_support():
    poly,view,members=face_scene();associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='projected_face_candidate'
    assert poly['native_support_resolved_ambiguity'] is True
    assert poly['edges'][0]['marks']==['base']
    assert poly['angular_candidate_count']==2


def test_two_supported_faces_remain_ambiguous_and_missing_support_stays_pending():
    poly,view,members=face_scene();members[1]['axis']={'a':[0,-10],'b':[100,-10]}
    associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='ambiguous'
    poly,view,members=face_scene();members.pop(0)
    associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='unresolved_native_extent'
    assert not poly['native_support']['all_edges_supported']


def test_native_support_does_not_fill_observed_gaps_or_accept_bom_fitted_extent():
    poly,view,members=face_scene();members.pop(0);far=members[0]
    far['visible_axis']=copy.deepcopy(far['axis']);far['axis']={'a':[0,-10],'b':[400,-10]}
    far['overlap_recovery']={'extensions':[{'native_edge_primitive_ids':['solid'],
        'visible_edge_intervals_pt':[[-310,-306],[-194,-190]],'inferred_intervals_pt':[[-306,-194]]}]}
    # The proposed face side is x=-5..105, entirely inside the unobserved gap.
    associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='unresolved_native_extent'
    far['overlap_recovery']['extensions'][0]['visible_edge_intervals_pt'].append([-290,-280])
    associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='projected_face_candidate'
    assert poly['native_support']['edges'][0]['overlap_pt']==pytest.approx(10)
    # Duplicate front/back observations are not counted twice.
    duplicate=copy.deepcopy(far);duplicate['id']='back';members.append(duplicate)
    associate_polygon(poly,projected_axes(members),view)
    assert poly['native_support']['edges'][0]['overlap_pt']==pytest.approx(10)


def test_finite_overlap_does_not_promote_a_worse_angle_candidate():
    poly,view,members=face_scene();members.pop(0)
    members.append({'id':'tilted','mark':'tilted','view_id':'v','axis':{'a':[0,-10],'b':[100,-1.25]}})
    associate_polygon(poly,projected_axes(members),view)
    assert poly['association_status']=='unresolved_native_extent'
    assert poly['angular_candidate_count']==1


def test_pending_span_records_identify_missing_end_and_label_ambiguity():
    edge,base,other=span_scene()
    for e in base['edges'][1:]:e.update(member_ids=[],axis_id='unused',direction=[0,1])
    labels=[text('s1','-5',(1,-4)),text('s2','-6',(2,-4))]
    record=span_bom_checks([base,other],{'v':labels},[{'id':'m','mark':'7','bom':{'length_mm':110}}])[0]
    assert record['status']=='basis_incomplete'
    assert {r['code'] for r in record['pending_reasons']}=={'end_0_ambiguous','end_1_missing'}
    assert 'computed_cut_length_mm' not in record


def test_zero_diagrams_or_incomplete_evidence_never_report_a_face_passed():
    views=[{'id':'empty','title':'empty'},{'id':'partial','title':'partial'}]
    poly={'view_id':'partial','association_status':'unresolved_native_extent','labels_complete':True}
    summaries=summarize_views(views,[poly],[],[],[])
    assert summaries[0]['status']=='no_dimension_diagram'
    assert summaries[1]['status']=='evidence_incomplete'
    assert summaries[1]['counts']['correspondence_pending']==1
    assert all(s['engineering_verified'] is False for s in summaries)


def test_original_bom_conflict_is_not_dropped_from_candidate_comparison():
    polygon,spans,adjacent=section11_decision_inputs()
    spans[0].update(status='end_basis_or_length_conflict',bom_length_mm=1290.)
    result=compare_dimension_hypotheses(polygon,spans,adjacent)
    assert result['bom_anchor_count']==1
    assert result['preferred'] is None
    # A correction can also resolve a previously conflicting BOM comparison.
    spans[0].update(span_text_id='t0',computed_cut_length_mm=987.,bom_length_mm=994.)
    result=compare_dimension_hypotheses(polygon,spans,[])
    assert result['preferred']['candidate_mm']==994
    assert result['preferred']['bom_checks'][0]['passed'] is True


def test_candidate_change_is_checked_when_it_is_an_input_to_a_neighbor():
    polygon,_,adjacent=section11_decision_inputs()
    check=adjacent[0];check['polygon_ids']=['neighbor','p'];check['target_text_id']='neighbor_target'
    check['observed_mm']=987.4;check['input_text_ids'][2]='t0'
    # The proposed 994 is now a source side in a neighboring equation.
    check['input_values_mm']['first_outer_side']=987.
    result=compare_dimension_hypotheses(polygon,[],[check])
    assert result['adjacent_triangle_check_count']==1
    candidate=next(h for h in result['hypotheses'] if h['candidate_mm']==994)
    assert candidate['triangle_checks'][0]['input_values_mm'][2]==994
    assert result['preferred'] is None
