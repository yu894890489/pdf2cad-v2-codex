import copy
import math

import pytest

from tower_rebuild.topology_features import circular_features, link_features
from tower_rebuild.topology_overlap import recover_member, view_scale, native_extent_scale, hidden_outline_tracks


def member(mid="rod",mark="501",length=140):
    return {"id":mid,"mark":mark,"page":1,"view_id":"face","side":"前","projection_group":"callout_"+mid,
            "axis":{"a":[0.,0.],"b":[100.,0.]},"polygon":[[0,-5],[100,-5],[100,5],[0,5]],
            "bom":{"length_mm":length,"quantity":1},"primitive_ids":["edge1","edge2"],"hole_ids":[]}


def stroke(pid,a,b):
    return {"id":pid,"a":a,"b":b,"length_pt":math.dist(a,b),"width_pt":.7,"primitive_ids":[pid]}


def holes():
    return [{"id":name,"center_pt":[x,0.],"radii_pt":[2.],"view_id":"face","member_ids":[],"plate_ids":[]}
            for name,x in (("left",-16.),("right",116.))]


def edges(start=0,end=100):
    return [stroke("edge1",[start,-5],[end,-5]),stroke("edge2",[start,5],[end,5]),
            stroke("plate-border1",[0,-5],[0,5]),stroke("plate-border2",[100,-5],[100,5])]


SCALE={"mm_per_pt_candidate":1.,"supporting_member_ids":["a","b","c"],"status":"test_independent_scale"}


def test_plate_borders_are_not_member_cut_ends():
    original=member();snapshot=copy.deepcopy(original)
    result=recover_member(original,edges(),[],holes(),[original],SCALE)
    assert result["applied"] and result["basis"]=="bom_and_end_hole_envelope"
    assert result["axis"]["a"]==pytest.approx([-20,0])
    assert result["axis"]["b"]==pytest.approx([120,0])
    assert [e["length_pt"] for e in result["extensions"]]==pytest.approx([20,20])
    assert result["hole_envelope_constraint"]["start_interval_pt"]==pytest.approx([-22,-18])
    assert not result["full_endpoints_verified"]
    assert original==snapshot  # Recovery does not rewrite BOM or the original ink.


def test_plate_collinear_edges_cannot_make_member_longer_than_stock():
    original=member()
    result=recover_member(original,edges(-90,200),[],holes(),[original],SCALE)
    assert result["applied"] and result["recovered_length_pt"]==pytest.approx(140)
    assert result["axis"]["a"]==pytest.approx([-20,0])


def test_bom_alone_does_not_guess_equal_extension_at_both_ends():
    result=recover_member(member(),edges(),[],[],[],SCALE)
    assert not result["applied"] and not result["extensions"]


def test_complete_member_is_not_lengthened_merely_because_plate_is_nearby():
    result=recover_member(member(length=100),edges(-30,130),[],holes(),[],SCALE)
    assert not result["applied"] and result["status"]=="already_matches_bom_candidate"


def test_partial_native_continuation_keeps_unresolved_length_visible():
    result=recover_member(member(length=180),edges(-10,100),[],[],[],SCALE)
    assert result["applied"] and result["status"]=="partial_overlap_recovered_candidate"
    assert result["recovered_length_pt"]==pytest.approx(110)
    assert result["remaining_length_pt_candidate"]==pytest.approx(70)
    assert not result["length_match_candidate"] and not result["full_endpoints_verified"]


def test_wide_end_position_interval_remains_ambiguous():
    result=recover_member(member(length=180),edges(),[],holes(),[],SCALE)
    assert not result["applied"] and result["status"]=="ambiguous_end_allocation"


def test_other_members_hole_row_is_not_borrowed_at_a_joint():
    stray={"id":"diagonal-hole","center_pt":[130,4],"radii_pt":[2.],"view_id":"face"}
    result=recover_member(member(),edges(),[],holes()+[stray],[],SCALE)
    assert result["applied"]
    assert "diagonal-hole" not in result["hole_envelope_constraint"]["hole_ids"]
    assert result["axis"]["a"]==pytest.approx([-20,0])


def test_recovery_cannot_absorb_a_different_collinear_member():
    other=member("other","502");other["axis"]={"a":[110,0],"b":[170,0]}
    result=recover_member(member(),edges(),[],holes(),[other],SCALE)
    assert not result["applied"]
    other["side"]="后"
    assert recover_member(member(),edges(),[],holes(),[other],SCALE)["applied"]


def test_overlap_geometry_can_belong_to_member_and_plate_simultaneously():
    m=member();r=recover_member(m,edges(),[],holes(),[],SCALE)
    m["axis"]=r["axis"];m["polygon"]=r["polygon"]
    p={"id":"plate","view_id":"face","side":"前","polygon":[[100,-10],[130,-10],[130,10],[100,10]],"hole_ids":[],"member_ids":[]}
    h=holes();link_features([m],[p],h)
    assert h[1]["member_ids"]==["rod"] and h[1]["plate_ids"]==["plate"]
    assert p["member_ids"]==["rod"] and "right" in m["hole_ids"]


def test_scale_needs_multiple_independent_observations():
    paired=[member("front"),member("rear")]
    paired[1]["projection_group"]=paired[0]["projection_group"]
    assert view_scale(paired) is None
    records=[member(str(i),str(i),100) for i in range(3)]
    assert view_scale(records)["mm_per_pt_candidate"]==1


def test_regular_dashed_hole_is_recovered_but_isolated_arcs_are_not():
    view={"id":"face","page":1,"role":"structural_view"}
    arcs=[]
    for i in range(17):
        arcs.append({"id":f"a{i}","primitive_type":"circular_arc","geometry":{"center":[0,0],"radius":3.,
                     "start_angle":math.tau*i/17,"sweep_angle":math.tau/17*.67}})
    observed,_=circular_features(view,arcs,[],[])
    assert len(observed)==1 and observed[0]["visibility"]=="hidden_dashed_circle_projection"
    assert observed[0]["diameter_mm"] is None and len(observed[0]["primitive_ids"])==17
    assert circular_features(view,arcs[:8],[],[])[0]==[]


def test_native_extent_excludes_neighbor_rows_beyond_both_cut_ends():
    foreign=[{"id":name,"center_pt":[x,3.],"radii_pt":[2.],"view_id":"face"}
             for name,x in (("top-chord",-28.),("bottom-chord",128.))]
    result=recover_member(member(),edges(-20,120),[],holes()+foreign,[],SCALE)
    assert result["applied"] and result["length_match_candidate"]
    assert set(result["excluded_neighbor_hole_ids"])=={"top-chord","bottom-chord"}
    assert result["axis"]["a"]==pytest.approx([-20,0])


def test_native_continuations_supply_scale_with_an_unextended_check():
    records=[];strokes=[]
    for i in range(3):
        m=member(str(i),str(i),100 if i==0 else 140)
        for point in list(m["axis"].values())+m["polygon"]:point[1]+=i*25
        records.append(m)
        for j,s in enumerate(edges(0 if i==0 else -20,100 if i==0 else 120)):
            for key in ("a","b"):s[key][1]+=i*25
            s["id"]=f"s{i}_{j}";s["primitive_ids"]=[s["id"]];strokes.append(s)
    assert view_scale(records) is None
    scale=native_extent_scale(records,{"strokes":strokes},[],[])
    assert scale["mm_per_pt_candidate"]==pytest.approx(1)
    assert len(scale["native_extent_evidence"])==2
    # Without the independent original-span check, native candidates cannot
    # collectively certify their own scale.
    assert native_extent_scale(records[1:],{"strokes":strokes},[],[]) is None


def hidden_fragments(sides=(-5,5)):
    primitives=[]
    for side in sides:
        for i in range(19):
            x=-20+i*1.05
            primitives.append({"id":f"hidden_{side}_{i}","primitive_type":"line","role":"dash_segment_candidate",
                               "geometry":{"start":[x,side],"end":[x+.7,side]},"style":{"width":.5}})
    return primitives


def test_paired_hidden_edges_restore_ink_but_keep_bom_conflict():
    hidden=hidden_outline_tracks(hidden_fragments())
    m=member(length=80)
    result=recover_member(m,edges(),[],holes(),[],SCALE,hidden)
    assert result["applied"] and result["status"]=="native_extent_bom_conflict_candidate"
    assert result["axis"]["a"]==pytest.approx([-20,0])
    assert result["recovered_length_pt"]==pytest.approx(120)
    assert result["bom_cut_length_mm"]==80 and m["bom"]["length_mm"]==80
    assert result["extensions"][0]["hidden_edge_primitive_ids"]
    assert not result["length_match_candidate"] and not result["full_endpoints_verified"]


def test_one_hidden_edge_or_reference_axis_does_not_override_bom():
    for sides in ((-5,),(0,)):
        result=recover_member(member(length=80),edges(),[],holes(),[],SCALE,hidden_outline_tracks(hidden_fragments(sides)))
        assert not result["applied"]


def test_partial_hidden_edge_does_not_skip_first_bolt_in_pitch_sequence():
    h=[{"id":str(x),"center_pt":[x,0.],"radii_pt":[2.],"view_id":"face"} for x in (-14,-4,104,114,128)]
    hidden=[dict(stroke(f"hidden{s}",[100,s],[105,s]),source_kind="paired_hidden_outline_evidence") for s in (-5,5)]
    result=recover_member(member(length=132),edges(),[],h,[],SCALE,hidden)
    assert result["applied"] and result["axis"]["a"]==pytest.approx([-16,0])
    assert "128" not in result["hole_envelope_constraint"]["hole_ids"]
