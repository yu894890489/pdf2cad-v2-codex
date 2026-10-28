import copy

import pytest

from tower_rebuild.topology import apply_part_review, apply_connection_review, member_connections, build_graph
from tower_rebuild.topology_features import circular_features, part_observations, link_features, check_bom_length_consistency
from tower_rebuild.topology_geometry import native_strokes, merge_collinear, parallel_bands, extend_supported_bands, classify_axis_contact
from tower_rebuild.topology_views import find_views


def primitive(pid, kind, geometry, **kwargs):
    return {"id":pid,"page":1,"primitive_type":kind,"geometry":geometry,"role":"unassigned_geometry",
            "line_style":"solid","style":{"width":.5},**kwargs}


def test_painted_strip_duplicates_are_not_two_steel_members():
    points=[[0,0],[100,0],[100,.7],[0,.7]]
    records=[primitive("stroke","quadrilateral",{"points":points}),primitive("fill","polygon",{"points":points+[points[0]]})]
    strokes=native_strokes(records)
    assert len(strokes)==1 and set(strokes[0]["primitive_ids"])=={"stroke","fill"}
    assert not parallel_bands(merge_collinear(strokes))


def test_dashed_candidates_do_not_enter_member_topology():
    records=[primitive("dash","line",{"start":[0,0],"end":[100,0]},role="dash_segment_candidate"),
             primitive("native","line",{"start":[0,10],"end":[100,10]},line_style="dashed")]
    assert native_strokes(records)==[]


def test_long_chord_bridges_opposing_edge_only_with_continuous_native_backbone():
    records=[primitive(str(i),"line",{"start":a,"end":b}) for i,(a,b) in enumerate([
        ([0,0],[300,0]),([0,10],[95,10]),([110,10],[210,10]),([225,10],[300,10])])]
    tracks=merge_collinear(native_strokes(records),gap=5)
    extended=extend_supported_bands(parallel_bands(tracks),tracks)
    full=[b for b in extended if "opposing_edge_coverage" in b]
    assert len(full)==1 and full[0]["length_pt"]==300
    assert .89 < full[0]["opposing_edge_coverage"] < .91
    sparse=records[:2]
    tracks=merge_collinear(native_strokes(sparse),gap=5)
    assert not any("opposing_edge_coverage" in b for b in extend_supported_bands(parallel_bands(tracks),tracks))


def test_crossing_is_not_a_physical_joint_and_endpoint_is_still_candidate():
    a={"a":[0,0],"b":[100,100]};b={"a":[0,100],"b":[100,0]}
    c=classify_axis_contact(a,b)
    assert c["kind"]=="crossing_without_connection_evidence" and c["connected"] is False
    b={"a":[100,100],"b":[200,0]}
    c=classify_axis_contact(a,b)
    assert c["kind"]=="endpoint_contact_candidate" and c["connected"] is False


def test_hole_excludes_callout_and_does_not_convert_radius_to_mm():
    view={"id":"v1","page":1,"role":"structural_view"}
    records=[primitive("circle","circle",{"center":[20,20],"radius":3}),
             primitive("ring","circle",{"center":[20,20],"radius":4}),
             primitive("label","circle",{"center":[40,40],"radius":5})]
    holes,_=circular_features(view,records,[{"circle_ids":["label"]}],[])
    assert len(holes)==1 and holes[0]["diameter_mm"] is None
    assert holes[0]["primitive_ids"]==["circle","ring"]
    records.append(primitive("dimension","text",{},text="Ø21.5",bbox_pt=[0,0,10,10]))
    holes,_=circular_features(view,records,[{"circle_ids":["label"]}],[])
    assert holes[0]["diameter_mm"] is None
    view["role"]="detail"
    holes,_=circular_features(view,records,[{"circle_ids":["label"]}],[])
    assert holes[0]["diameter_mm"]==21.5 and holes[0]["diameter_evidence"]==["dimension"]
    records[-1]["text"]="2Ø21.5"
    assert circular_features(view,records,[{"circle_ids":["label"]}],[])[0][0]["diameter_mm"] is None


def test_front_rear_marks_remain_separate_and_bom_material_wins():
    view={"id":"v1","page":1,"callout_ids":["c"],"role":"structural_view"}
    callouts=[{"id":"c","bbox_pt":[35,-20,45,-10],"circle_ids":["circle"],"text":"前501 后502",
               "marks":[{"mark":m,"material_kind":"angle","side":side,"text_id":m} for m,side in [("501","前"),("502","后")]]}]
    bom={m:{"classification":{"kind":"angle"},"specification":"Q345L50X5","quantity":1,"length_mm":1000} for m in ("501","502")}
    bands=[{"id":"b1","a":[0,0],"b":[100,0],"width_pt":10,"length_pt":100,"polygon":[[0,-5],[100,-5],[100,5],[0,5]],"primitive_ids":["edge1","edge2"]}]
    members,plates,unknown=part_observations(view,callouts,bom,bands,[],[],[])
    assert len(members)==2 and not plates and not unknown
    assert members[0]["side"]!=members[1]["side"] and members[0]["projection_group"]==members[1]["projection_group"]
    assert member_connections(members,plates)==[]
    bom["502"]["classification"]["kind"]="plate"
    members,plates,_=part_observations(view,callouts,bom,bands,[],[],[])
    assert len(members)==len(plates)==1 and plates[0]["mark"]=="502"


def test_projected_hole_ownership_never_crosses_views():
    part={"id":"m1","view_id":"v1","polygon":[[0,0],[100,0],[100,10],[0,10]],"hole_ids":[]}
    holes=[{"id":"h1","view_id":"v2","center_pt":[20,5],"member_ids":[],"plate_ids":[]}]
    link_features([part],[],holes)
    assert not part["hole_ids"] and holes[0]["owner_status"]=="unresolved"
    holes[0]["view_id"]="v1"
    link_features([part],[],holes)
    assert part["hole_ids"]==["h1"] and holes[0]["member_ids"]==["m1"]


def test_review_cannot_confirm_connections_before_member_associations():
    members=[{"id":"m1","status":"proximity_candidate"},{"id":"m2","status":"proximity_candidate"}]
    connections=[{"id":"c1","member_ids":["m1","m2"],"connected":False}]
    review={"connections":[{"id":"c1","member_ids":["m1","m2"],"connected":True,"reason":"checked endpoints on original view"}]}
    with pytest.raises(ValueError,match="Confirm member"):
        apply_connection_review(review,connections,members)
    for m in members:m["status"]="reviewed_2d_association"
    apply_connection_review(review,connections,members)
    assert connections[0]["connected"] is True
    review["connections"][0]["member_ids"]=["other","m2"]
    with pytest.raises(ValueError,match="member IDs changed"):
        apply_connection_review(review,connections,members)


def test_review_rejects_geometry_from_another_face():
    m={"id":"m1","view_id":"v1"};b={"id":"b1","view_id":"v2"}
    with pytest.raises(ValueError,match="same-view"):
        apply_part_review({"parts":[{"id":"m1","geometry_id":"b1","reason":"test"}]},[m],[],[b],[],[])


def test_bom_ratio_flags_partial_axis_without_changing_geometry():
    members=[{"id":f"m{i}","projection_group":str(i),"bom":{"length_mm":700},"axis":{"a":[0,0],"b":[100 if i<5 else 45,0]}} for i in range(6)]
    before=copy.deepcopy(members)
    diagnostic=check_bom_length_consistency(members)
    assert diagnostic["median_cut_length_to_projection_ratio"]==7
    assert members[-1]["bom_projection_check"]["status"]=="possible_partial_or_wrong_axis"
    assert members[-1]["axis"]==before[-1]["axis"]
    assert members[-1]["bom_projection_check"]["verified_dimension"] is False


def test_thin_section_is_seeded_by_native_caption_not_ink_count(tmp_path):
    from PIL import Image, ImageDraw
    bitmap=Image.new("RGB",(400,400),"white");draw=ImageDraw.Draw(bitmap)
    draw.rectangle((200,50,202,190),outline="black",width=1)
    draw.text((190,208),"5--5",fill="black")
    bitmap.save(tmp_path/"structure.png")
    records=[primitive("body","line",{"start":[201,50],"end":[201,190]},bbox_pt=[200,50,202,190]),
             primitive("caption","text",{},text="5--5",bbox_pt=[190,205,220,220],text_block=0,text_line=0,text_word=0)]
    views,assignment,unassigned=find_views({"pages":[{"page":1,"size_pt":[400,400],"structure":"structure.png"}]},records,[],tmp_path)
    assert len(views)==1 and views[0]["title"]=="剖面 5—5"
    assert set(assignment)=={"body","caption"} and not unassigned
