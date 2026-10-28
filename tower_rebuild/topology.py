"""Build auditable, planar topology proposals from saved native PDF primitives."""
import itertools
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .io import read_json, write_json, file_digest, digest
from .topology_geometry import native_strokes, merge_collinear, parallel_bands, extend_supported_bands, classify_axis_contact
from .topology_views import find_views
from .topology_features import circular_features, contour_candidates, part_observations, link_features, check_bom_length_consistency
from .topology_overlap import recover_overlaps, hidden_outline_tracks


def prefix_geometry(view_id, strokes, tracks, bands):
    for records in (strokes,tracks,bands):
        for record in records:
            record["id"]=f"{view_id}_{record['id']}"
            record["view_id"]=view_id
            for key in ("stroke_ids","outline_ids","supporting_band_ids"):
                if key in record:
                    record[key]=[f"{view_id}_{value}" for value in record[key]]


def member_connections(members, plates):
    connections=[]
    for first,second in itertools.combinations(members,2):
        if first["view_id"]!=second["view_id"] or not first["axis"] or not second["axis"]:
            continue
        if first["projection_group"]==second["projection_group"] or first["band_id"]==second["band_id"]:
            continue
        if first.get("side") and second.get("side") and first["side"]!=second["side"]:
            continue
        widths=[]
        for member in (first,second):
            polygon=member.get("polygon")
            if polygon:
                import math
                widths.append(math.dist(polygon[0],polygon[-1]))
        tolerance=max(8.,min(32.,sum(widths)/2+8))
        contact=classify_axis_contact(first["axis"],second["axis"],tolerance=tolerance)
        if not contact:
            continue
        shared=sorted(set(first["hole_ids"])&set(second["hole_ids"]))
        connectors=[p["id"] for p in plates if first["id"] in p["member_ids"] and second["id"] in p["member_ids"]]
        if shared:
            contact["kind"]="shared_projected_hole_candidate"
        elif connectors:
            contact["kind"]="shared_plate_candidate"
        connections.append({"id":f"connection_{len(connections)+1:04d}","view_id":first["view_id"],
                            "member_ids":[first["id"],second["id"]],**contact,"hole_ids":shared,"plate_ids":connectors,"endpoint_tolerance_pt":tolerance,
                            "status":"not_a_connection" if contact["kind"]=="crossing_without_connection_evidence" else "needs_review",
                            "evidence_dependency":"member outline associations and front/rear ownership must be verified",
                            "primitive_ids":sorted(set(first["primitive_ids"]+second["primitive_ids"]))})
    return connections


def apply_part_review(review, members, plates, bands, contours, views):
    """Explicit reviewed associations are reproducible; no silent automatic approval."""
    lookup={p["id"]:p for p in members+plates}
    geometry={p["id"]:p for p in bands+contours}
    for decision in review.get("parts",[]):
        part=lookup.get(decision["id"]); selected=geometry.get(decision["geometry_id"])
        if part is None or selected is None or part["view_id"]!=selected["view_id"] or not decision.get("reason"):
            raise ValueError("Review part must reference same-view native geometry and include a reason")
        if part in members:
            if "a" not in selected:
                raise ValueError("Member review requires a parallel band")
            part.update(band_id=selected["id"],axis={"a":selected["a"],"b":selected["b"]},ambiguous=False)
        else:
            if "area_pt2" not in selected:
                raise ValueError("Plate review requires a contour")
            part["contour_id"]=selected["id"]
        part.update(polygon=selected["polygon"],status="reviewed_2d_association",review_reason=decision["reason"])
        part["primitive_ids"]=sorted(set(part["callout_primitive_ids"]+selected["primitive_ids"]))
    view_map={v["id"]:v for v in views}
    for decision in review.get("views",[]):
        if decision["id"] not in view_map or not decision.get("reason"):
            raise ValueError("Review view must exist and include a reason")
        if decision["face_role"] not in {"front","rear","top","bottom","section","detail","schematic"}:
            raise ValueError("Unknown reviewed face role")
        view_map[decision["id"]].update(face_role=decision["face_role"],face_role_status="reviewed",review_reason=decision["reason"])


def apply_connection_review(review, connections, members):
    lookup={c["id"]:c for c in connections}; member_map={m["id"]:m for m in members}
    for decision in review.get("connections",[]):
        c=lookup.get(decision["id"])
        if c is None or type(decision.get("connected")) is not bool or not decision.get("reason"):
            raise ValueError("Connection review requires existing ID, Boolean connected and reason")
        if set(decision.get("member_ids",[]))!=set(c["member_ids"]):
            raise ValueError("Connection review member IDs changed; regenerate and inspect the connection")
        if decision["connected"] and any(member_map[mid]["status"]!="reviewed_2d_association" for mid in c["member_ids"]):
            raise ValueError("Confirm member associations before confirming their connection")
        c.update(connected=decision["connected"],status="reviewed_connected" if decision["connected"] else "reviewed_disconnected",review_reason=decision["reason"])


def build_graph(views, members, plates, holes, connections):
    nodes=[]; edges=[]
    for category,items in (("view",views),("member_observation",members),("plate_observation",plates),("hole_projection",holes)):
        for p in items:
            nodes.append({"id":p["id"],"type":category,"view_id":p.get("view_id",p["id"]),
                          "mark":p.get("mark"),"side":p.get("side"),"status":p.get("status"),
                          "point":p.get("center_pt") or ([(p["axis"]["a"][k]+p["axis"]["b"][k])/2 for k in (0,1)] if p.get("axis") else None)})
            if category!="view":
                edges.append({"source":p["view_id"],"target":p["id"],"type":"view_contains_observation","status":"derived"})
    for c in connections:
        edges.append({"source":c["member_ids"][0],"target":c["member_ids"][1],"type":c["kind"],
                      "status":c["status"],"connected":c["connected"],"connection_id":c["id"]})
    for h in holes:
        for owner in h["member_ids"]+h["plate_ids"]:
            edges.append({"source":owner,"target":h["id"],"type":"projected_hole_containment","status":h["owner_status"]})
    for p in plates:
        for mid in p["member_ids"]:
            edges.append({"source":mid,"target":p["id"],"type":"shared_projected_hole_with_plate","status":"needs_review"})
    groups=defaultdict(list)
    for p in members+plates:
        groups[p["mark"]].append(p)
    correspondences=[]
    for mark,parts in sorted(groups.items()):
        if len({p["view_id"] for p in parts})<2:
            continue
        correspondences.append({"mark":mark,"observation_ids":[p["id"] for p in parts],
                                "view_ids":sorted({p["view_id"] for p in parts}),
                                "status":"same_bom_mark_not_proof_of_same_physical_instance"})
    return {"schema_version":"1.0","coordinate_space":"PDF points, separate planar views",
            "nodes":nodes,"edges":edges,"cross_view_correspondences":correspondences,
            "physical_connections":[c["id"] for c in connections if c["connected"]]}


def topology(run_dir, review_path=None):
    run_dir=Path(run_dir); site=run_dir/"site"; destination=site/"topology"
    summary=read_json(site/"summary.json")
    if summary.get("stage")!="tables_and_primitives":
        raise ValueError("Run decompose first; topology requires saved table/primitive arrays")
    manifest=read_json(run_dir/"manifest.json")
    from .ocr_input import analysis_source
    analysis_source(manifest)
    if manifest["source_sha256"]!=summary["source_sha256"]:
        raise ValueError("Manifest and decomposition source hashes disagree")
    if Path(manifest["source"]).exists() and file_digest(manifest["source"])!=summary["source_sha256"]:
        raise ValueError("Source PDF changed; run decompose again")
    inputs={name:file_digest(site/name) for name in ("summary.json","tables/bom.json","structure/primitives.json","structure/callouts.json")}
    input_hash=digest(inputs)
    primitives=read_json(site/"structure/primitives.json"); callouts=read_json(site/"structure/callouts.json")
    text_stroke_ids = [p['id'] for p in primitives if p.get('excluded_from_structure')]
    primitives = [p for p in primitives if not p.get('excluded_from_structure')]
    bom={r["mark"]:r for r in read_json(site/"tables/bom.json")}
    review=read_json(review_path) if review_path else {}
    if review and (review.get("source_sha256")!=summary["source_sha256"] or review.get("input_bundle_sha256")!=input_hash):
        raise ValueError("Review must match both source and decomposed input hashes")
    print("[topology] grouping native primitives into individual views",flush=True)
    views,assignment,unassigned=find_views(summary,primitives,callouts,site)
    primitive_map={p["id"]:p for p in primitives}
    members=[];plates=[];holes=[];bands=[];contours=[];unknown=[];view_geometry=[]
    for view in views:
        contents=[primitive_map[pid] for pid in view["primitive_ids"]]
        strokes=native_strokes(contents);tracks=merge_collinear(strokes,gap=35);vb=extend_supported_bands(parallel_bands(tracks),tracks)
        prefix_geometry(view["id"],strokes,tracks,vb)
        vh,annotations=circular_features(view,contents,callouts,strokes)
        vc=contour_candidates(view,strokes,vh)
        vm,vp,vu=part_observations(view,callouts,bom,vb,vc,vh,strokes)
        members.extend(vm);plates.extend(vp);holes.extend(vh);bands.extend(vb);contours.extend(vc);unknown.extend(vu)
        view.update(face_role="schematic" if view["role"]=="front_schematic" else "section" if view["role"]=="section_view" else "detail" if view["role"]=="detail" else "unassigned",
                    face_role_status="drawing_label" if view["title_evidence"] else "needs_review",
                    spatial_transform=None,member_ids=[m["id"] for m in vm],plate_ids=[p["id"] for p in vp],hole_ids=[h["id"] for h in vh],
                    diameter_annotations=annotations,excluded_dash_primitive_count=sum(p["role"]=="dash_segment_candidate" or p.get("line_style")=="dashed" for p in contents))
        view_geometry.append({"view_id":view["id"],"strokes":strokes,"outlines":tracks,"bands":vb,"contours":vc,
                              "hidden_outlines":hidden_outline_tracks(contents,vm)})
        print(f"[view] {view['title']}: {len(vm)} member observations, {len(vp)} plate observations, {len(vh)} circular projections",flush=True)
    geometry_hash=digest({"views":[{"id":v["id"],"primitive_ids":sorted(v["primitive_ids"])} for v in views],
                          "bands":bands,"contours":contours,"holes":holes,
                          "overlap_code_sha256":file_digest(Path(__file__).with_name("topology_overlap.py")),
                          "observations":[{k:p[k] for k in ("id","mark","callout_id")} for p in members+plates]})
    if review and review.get("geometry_sha256")!=geometry_hash:
        raise ValueError("Derived geometry changed; regenerate review from current geometry before confirming associations")
    apply_part_review(review,members,plates,bands,contours,views)
    for view in views:
        view["bom_length_diagnostic"]=check_bom_length_consistency([m for m in members if m["view_id"]==view["id"]])
    overlaps=recover_overlaps(members,plates,holes,view_geometry)
    print(f"[overlap] {sum(m['overlap_recovery']['applied'] for m in members)} member observations recovered; original axes retained",flush=True)
    link_features(members,plates,holes)
    connections=member_connections(members,plates)
    apply_connection_review(review,connections,members)
    graph=build_graph(views,members,plates,holes,connections)
    issues=[]
    def issue(code,ids,message,view_id=None):
        if ids:
            issues.append({"id":f"issue_{len(issues)+1:03d}","code":code,"view_id":view_id,"object_ids":ids,"message":message})
    issue("unassigned_primitives",unassigned,"这些图元尚未归入视图；保留原数据。")
    assigned_callouts={cid for v in views for cid in v["callout_ids"]}
    issue("unassigned_callouts",[c["id"] for c in callouts if c["id"] not in assigned_callouts],"件号圆圈未归入视图，不能遗漏为已完成。")
    for v in views:
        vid=v["id"]
        if v["face_role"]=="unassigned":
            issue("face_role_unconfirmed",[vid],"尚需核实本视图对应前、后、上或下结构面；没有推断空间朝向。",vid)
        issue("member_association_unconfirmed",[m["id"] for m in members if m["view_id"]==vid and m["status"]!="reviewed_2d_association"],"杆件轮廓对应为候选；须核实引线、端点及 BOM 下料长度。",vid)
        issue("bom_length_projection_mismatch",[m["id"] for m in members if m["view_id"]==vid and not m["overlap_recovery"].get("length_match_candidate") and m.get("bom_projection_check",{}).get("status")=="possible_partial_or_wrong_axis"],"候选轴线长度与本面多数杆件的 BOM/投影比例不一致，可能只取到一段或关联到错误轮廓；尚未通过尺寸校核。",vid)
        issue("overlap_extent_candidate",[m["id"] for m in members if m["view_id"]==vid and m["overlap_recovery"]["applied"]],"已按原生边线、孔排与 BOM 补回重叠段；原识别段单独保存，完整端点仍为候选，位置余量见 overlap_recovery。",vid)
        issue("overlap_extent_partial",[m["id"] for m in members if m["view_id"]==vid and m["overlap_recovery"]["status"]=="partial_overlap_recovered_candidate"],"目前只补回有原生边线支持的部分，仍有缺长未解决；不能将此候选当作完整杆件。",vid)
        issue("native_extent_bom_conflict",[m["id"] for m in members if m["view_id"]==vid and m["overlap_recovery"].get("bom_conflict")],"按两侧隐藏轮廓及孔位接续了原图杆身，但投影全长与圈号对应 BOM 冲突；件号及尺寸需核对，BOM 原值未改写。",vid)
        issue("overlap_extent_unresolved",[m["id"] for m in members if m["view_id"]==vid and m["overlap_recovery"]["status"] in {"extent_unresolved","ambiguous_end_allocation","hole_span_conflicts_with_bom","native_extent_alternative_without_scale"}],"这些杆件尚不能唯一确定板下延伸范围；未把板边当作已确认杆端，也未用 BOM 均分缺长。",vid)
        issue("plate_boundary_unconfirmed",[p["id"] for p in plates if p["view_id"]==vid and p["status"]!="reviewed_2d_association"],"连接板件号已查 BOM，轮廓及所属接点仍需核对。",vid)
        issue("hole_owner_unconfirmed",[h["id"] for h in holes if h["view_id"]==vid],"圆形投影可能为孔或螺栓符号；所属零件、孔径和孔距需要图纸证据，不能用 PDF 点换算。",vid)
    observed_marks={m["mark"] for m in members}
    missing_marks=sorted(mark for mark,row in bom.items() if row["classification"]["kind"]=="angle" and mark not in observed_marks)
    issue("bom_member_mark_not_observed",missing_marks,"BOM 杆件件号未找到圈号观察；不代表可以省略该杆件。")
    issue("callout_mark_not_in_bom",[u["callout_id"] for u in unknown],"圈号数字不在本页 BOM 中，未作为杆件或连接板。")
    node_ids={n["id"] for n in graph["nodes"]}
    all_ids=[p["id"] for p in primitives]
    checks={"primitive_partition_complete":set(assignment)|set(unassigned)==set(all_ids),
            "primitive_partition_disjoint":not (set(assignment)&set(unassigned)),
            "every_graph_edge_resolves":all(e["source"] in node_ids and e["target"] in node_ids for e in graph["edges"]),
            "evidence_resolves":all(pid in primitive_map for p in members+plates+holes for pid in p["primitive_ids"]),
            "all_members_are_bom_angles":all(bom[m["mark"]]["classification"]["kind"]=="angle" for m in members),
            "all_plates_are_bom_plates":all(bom[p["mark"]]["classification"]["kind"]=="plate" for p in plates),
            "unreviewed_crossings_are_disconnected":all(not c["connected"] for c in connections if c["kind"]=="crossing_without_connection_evidence" and not c["status"].startswith("reviewed_")),
            "no_cross_view_hole_attachment":all(next(p for p in members+plates if p["id"]==pid)["view_id"]==h["view_id"] for h in holes for pid in h["member_ids"]+h["plate_ids"])}
    checks["original_axes_retained"]=all(m.get("visible_axis") is not None for m in members if m.get("axis"))
    checks["overlap_never_changes_bom_length"]=all(m["bom"]["length_mm"]==bom[m["mark"]]["length_mm"] for m in members)
    checks["inferred_ends_not_automatically_verified"]=all(not m["overlap_recovery"].get("full_endpoints_verified",False) for m in members)
    if not all(checks.values()):
        raise ValueError(f"Topology integrity checks failed: {checks}")
    report={"schema_version":"1.0","stage":"planar_topology_review","status":"needs_review","title":summary["title"],
            "source_sha256":summary["source_sha256"],"input_bundle_sha256":input_hash,"geometry_sha256":geometry_hash,"input_files":inputs,
            "coordinate_system":summary["coordinate_system"],"pages":summary["pages"],
            "counts":{"views":len(views),"member_observations":len(members),"member_marks":len(observed_marks),
                      "bom_member_marks":sum(r["classification"]["kind"]=="angle" for r in bom.values()),
                      "bom_member_quantity":sum(r["quantity"] for r in bom.values() if r["classification"]["kind"]=="angle"),
                      "plate_observations":len(plates),"circular_projections":len(holes),
                      "hidden_circle_projections":sum(h["visibility"]=="hidden_dashed_circle_projection" for h in holes),
                      "overlap_recovered_observations":sum(m["overlap_recovery"]["applied"] for m in members),
                      "overlap_partial_observations":sum(m["overlap_recovery"]["status"]=="partial_overlap_recovered_candidate" for m in members),
                      "overlap_bom_conflict_observations":sum(bool(m["overlap_recovery"].get("bom_conflict")) for m in members),
                      "explicit_diameter_projections":sum(h["diameter_mm"] is not None for h in holes),
                      "connection_candidates":sum(c["status"]=="needs_review" for c in connections),
                      "unconnected_crossings":sum(c["status"]=="not_a_connection" for c in connections),
                      "reviewed_connections":sum(c["connected"] for c in connections),"cross_view_mark_groups":len(graph["cross_view_correspondences"]),
                      "unassigned_primitives":len(unassigned), "excluded_text_strokes":len(text_stroke_ids)},
            "excluded_text_stroke_ids":text_stroke_ids,
            "checks":checks,"issues":issues,"missing_bom_member_marks":missing_marks,"unknown_callout_marks":unknown,
            "limitations":["这是二维证据与候选拓扑；尚未验证空间装配和所有实体连接。",
                           "一个件号可以出现在多个面；观察记录数不等于实物数量。前后件号分别保留。",
                           "视图分组采用连通墨迹和邻近区域，可能需要人工调整边界。",
                           "平行轮廓可能含尺寸线；没有复核的候选不能进入最终三维模型。",
                           "规则分段圆用于隐藏孔/螺栓投影；孤立圆弧、椭圆、长圆孔仍未完整解析。",
                           "重叠补全保留原识别段、补段依据与端点余量；BOM 约束满足不等于独立尺寸验收通过。",
                           "虚线不单独生成杆件；已有杆件两侧成对的短线轮廓，仅在孔位支持下用于恢复遮挡段。"]}
    exports={"report":report,"views":views,"members":members,"connections":connections,"plates":plates,"holes":holes,"overlaps":overlaps,
             "graph":graph,"geometry":view_geometry,"unassigned":{"primitive_ids":unassigned},
             "review-template":{"source_sha256":summary["source_sha256"],"input_bundle_sha256":input_hash,"geometry_sha256":geometry_hash,"views":[],"parts":[],"connections":[]}}
    for name,data in exports.items():
        write_json(destination/f"{name}.json",data)
    for asset in (Path(__file__).parent/"topology_web").iterdir():
        if asset.is_file():
            shutil.copy2(asset,destination/asset.name)
    index=site/"index.html"
    html=index.read_text(encoding="utf-8")
    if 'id="topology-link"' not in html:
        html=html.replace('</nav>','<a id="topology-link" href="topology/">逐面拓扑 · 杆件 / 连接板 / 孔位</a></nav>',1)
        index.write_text(html,encoding="utf-8")
    (run_dir/"failure.json").unlink(missing_ok=True)
    print(f"[topology] saved {len(views)} views; {len(connections)} contact/crossing records; status=needs_review",flush=True)
    return report
