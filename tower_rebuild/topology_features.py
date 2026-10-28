"""Evidence-preserving member, plate and circular-feature observations in 2D."""
import math
import re
from collections import defaultdict

import numpy as np
from shapely import node, polygonize
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from scipy.spatial import cKDTree

from .topology_geometry import point_segment_distance
from .topology_views import center


def circular_features(view, primitives, callouts, strokes):
    """Native circles may depict a hole or a bolt. Never infer drilled diameter."""
    excluded = {pid for c in callouts for pid in c["circle_ids"]}
    circles = [p for p in primitives if p["primitive_type"] == "circle" and p["id"] not in excluded
               and .25 <= p["geometry"]["radius"] <= 8]
    groups = []
    for p in circles:
        g = p["geometry"]
        group = next((h for h in groups if math.dist(h["center_pt"], g["center"]) < .2), None)
        if group is None:
            group = {"center_pt": g["center"], "radii_pt": [], "primitive_ids": []}
            groups.append(group)
        group["radii_pt"].append(g["radius"])
        group["primitive_ids"].append(p["id"])
    # Hidden fastener/holes in this CAD export are many short circular arcs,
    # not PDF circle objects. Recover only a distributed dashed-circle pattern;
    # an isolated semicircle remains an unresolved arc.
    arc_groups=[]
    for p in primitives:
        if p["primitive_type"]!="circular_arc" or p["id"] in excluded:continue
        g=p["geometry"]
        if not .25<=g["radius"]<=8:continue
        group=next((item for item in arc_groups if math.dist(item["center"],g["center"])<.08
                    and abs(item["radius"]-g["radius"])<.08),None)
        if group is None:
            group={"center":g["center"],"radius":g["radius"],"items":[]};arc_groups.append(group)
        group["items"].append(p)
    for group in arc_groups:
        if len(group["items"])<8:continue
        coverage=np.zeros(720,dtype=bool)
        for p in group["items"]:
            g=p["geometry"];count=max(3,math.ceil(abs(g["sweep_angle"])*720/math.tau)*2)
            angles=(g["start_angle"]+np.linspace(0,g["sweep_angle"],count))%math.tau
            coverage[(angles/math.tau*720).astype(int)%720]=True
        covered=np.flatnonzero(coverage)
        if len(covered)<360 or max(np.diff(np.r_[covered,covered[0]+720]))>72:continue
        h=next((h for h in groups if math.dist(h["center_pt"],group["center"])<.2),None)
        if h is None:
            h={"center_pt":group["center"],"radii_pt":[],"primitive_ids":[],"visibility":"hidden_dashed_circle_projection"};groups.append(h)
        h["radii_pt"].append(group["radius"])
        h["primitive_ids"].extend(p["id"] for p in group["items"])
        h["native_arc_coverage_fraction"]=float(coverage.mean())
    annotations = []
    for p in primitives:
        if p["primitive_type"] != "text":
            continue
        match = re.fullmatch(r"(?:(\d+)\s*)?[ØøΦφ⌀]\s*(\d+(?:\.\d+)?)(?:孔)?", p["text"].strip())
        if match:
            annotations.append({"primitive_id": p["id"], "count": int(match[1] or 1),
                                "diameter_mm": float(match[2]), "bbox_pt": p["bbox_pt"]})
    for i, h in enumerate(groups, 1):
        crosshair = []
        for s in strokes:
            distance, t = point_segment_distance(h["center_pt"], s["a"], s["b"])
            if distance < .35 and .08 < t < .92 and s["length_pt"] < max(h["radii_pt"])*5+8:
                crosshair.extend(s["primitive_ids"])
        h.update(id=f"{view['id']}_hole_{i:03d}", view_id=view["id"], page=view["page"],
                 kind="circular_hole_or_fastener_projection", status="needs_review", diameter_mm=None,
                 diameter_evidence=[], crosshair_primitive_ids=sorted(set(crosshair)), member_ids=[], plate_ids=[],
                 owner_status="unresolved", physical_hole_id=None)
        h.setdefault("visibility","solid_circle_projection")
        # A detail's explicit count must agree; shared values in an assembly are not
        # propagated across all circles or converted from PDF radius.
        if view["role"] == "detail" and len(annotations) == 1 and annotations[0]["count"] == len(groups):
            h["diameter_mm"] = annotations[0]["diameter_mm"]
            h["diameter_evidence"] = [annotations[0]["primitive_id"]]
            h["status"] = "detail_diameter_annotation_supported"
    return groups, annotations


def contour_candidates(view, strokes, holes):
    """Polygonize drawn outlines for plate proposals, not structural joints."""
    if not strokes:
        return []
    # Stroke centres at painted corners differ slightly. Snap only endpoints;
    # keep the original primitives and report this derived tolerance explicitly.
    endpoints=np.array([p for s in strokes for p in (s["a"],s["b"])],dtype=float)
    parent=list(range(len(endpoints)))
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    for a,b in cKDTree(endpoints).query_pairs(.65):
        parent[root(b)]=root(a)
    groups=defaultdict(list)
    for i in range(len(endpoints)):groups[root(i)].append(i)
    for indices in groups.values():endpoints[indices]=endpoints[indices].mean(axis=0)
    lines = MultiLineString([(endpoints[i],endpoints[i+1]) for i in range(0,len(endpoints),2)
                             if np.linalg.norm(endpoints[i+1]-endpoints[i])>1e-8])
    # Node geometry solely to find closed regions. It never asserts connectivity.
    polygons = polygonize(list(node(lines).geoms))
    result = []
    for polygon in polygons.geoms:
        if not 12 <= polygon.area <= 18000:
            continue
        rectangle = polygon.minimum_rotated_rectangle
        points = list(rectangle.exterior.coords)
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        if min(lengths) < 2 or max(lengths)/min(lengths) > 14:
            continue
        enclosed = [h["id"] for h in holes if polygon.buffer(.4).covers(Point(h["center_pt"]))]
        if not enclosed:
            continue
        evidence = [pid for s in strokes if polygon.boundary.distance(LineString([s["a"],s["b"]])) < .3
                    and polygon.boundary.intersection(LineString([s["a"],s["b"]]).buffer(.2)).length > 1
                    for pid in s["primitive_ids"]]
        result.append({"id": f"{view['id']}_contour_{len(result)+1:03d}", "view_id": view["id"],
                       "polygon": [list(p) for p in polygon.exterior.coords], "area_pt2": polygon.area,
                       "hole_ids": enclosed, "primitive_ids": sorted(set(evidence)),
                       "endpoint_snap_tolerance_pt":.65,
                       "status": "closed_ink_region_not_yet_plate"})
    return result


def leader_support(callout, shape, strokes):
    """A drawn leader from circle rim to geometry, excluding pass-through lines."""
    box = callout["bbox_pt"]; c = center(box); radius = (box[2]-box[0]+box[3]-box[1])/4
    evidence = []
    for s in strokes:
        if not 3 <= s["length_pt"] <= 180:
            continue
        for a,b in ((s["a"],s["b"]),(s["b"],s["a"])):
            if abs(math.dist(a,c)-radius) > 2.2 or math.dist(b,c) < radius+3:
                continue
            if shape.distance(Point(b)) < 2:
                evidence.extend(s["primitive_ids"])
    return sorted(set(evidence))


def member_candidates(callout, bands, holes, strokes):
    c = center(callout["bbox_pt"])
    radius = (callout["bbox_pt"][2]-callout["bbox_pt"][0])/2
    choices = []
    for band in bands:
        d,t = point_segment_distance(c,band["a"],band["b"])
        clearance = d-radius-band["width_pt"]/2
        if clearance > 90 or not -.3 <= t <= 1.3:
            continue
        polygon = Polygon(band["polygon"])
        leader = leader_support(callout, polygon, strokes)
        # At least some holes or a leader help discriminate dimension-line pairs.
        inside = [h["id"] for h in holes if polygon.buffer(.4).covers(Point(h["center_pt"]))]
        score = max(0,clearance) + max(0,-clearance-3)*2 + (0 if inside else 12) - (32 if leader else 0)
        if band.get("opposing_edge_coverage"):
            score -= 10
        if t < .05 or t > .95:
            score += 12
        choices.append({"band_id":band["id"],"score":round(score,3),"distance_pt":d,
                        "leader_primitive_ids":leader,"hole_ids":inside,
                        "basis":"native_parallel_outlines + leader" if leader else "native_parallel_outlines + proximity_only"})
    choices.sort(key=lambda c:(c["score"], c["band_id"]))
    return choices[:5]


def part_observations(view, callouts, bom, bands, contours, holes, strokes):
    members, plates, unknown = [], [], []
    band_by_id = {b["id"]:b for b in bands}
    for c in callouts:
        if c["id"] not in view["callout_ids"]:
            continue
        choices = member_candidates(c,bands,holes,strokes) if any(m["material_kind"] == "angle" for m in c["marks"]) else []
        for mark in c["marks"]:
            row = bom.get(mark["mark"])
            if row is None:
                unknown.append({"view_id":view["id"],"callout_id":c["id"],"mark":mark["mark"],"reason":"mark_not_in_bom"})
                continue
            kind = row["classification"]["kind"]
            if kind not in {"angle","plate"}:
                continue
            part = {"id": f"{view['id']}_{'member' if kind=='angle' else 'plate'}_{len(members if kind=='angle' else plates)+1:03d}",
                    "view_id":view["id"],"page":view["page"],"mark":mark["mark"],"side":mark.get("side"),
                    "callout_id":c["id"],"callout_bbox_pt":c["bbox_pt"],"callout_text":c["text"],
                    "primitive_ids":c["circle_ids"]+[mark["text_id"]],"bom":row,
                    "callout_primitive_ids":c["circle_ids"]+[mark["text_id"]],
                    "physical_instance_id":None,"status":"unresolved", "hole_ids":[]}
            if kind == "angle":
                part.update(candidate_bands=choices, band_id=None, axis=None, polygon=None,
                            projection_group=f"{view['id']}_{c['id']}")
                if choices:
                    best=choices[0]; b=band_by_id[best["band_id"]]
                    part.update(band_id=b["id"],axis={"a":b["a"],"b":b["b"]},polygon=b["polygon"],
                                association_basis=best["basis"],leader_primitive_ids=best["leader_primitive_ids"],
                                status="leader_supported_candidate" if best["leader_primitive_ids"] else "proximity_candidate",
                                ambiguous=len(choices)>1 and choices[1]["score"]-best["score"]<8)
                    part["primitive_ids"] = sorted(set(part["primitive_ids"]+b["primitive_ids"]+best["leader_primitive_ids"]))
                members.append(part)
            else:
                pc=[]
                for contour in contours:
                    polygon=Polygon(contour["polygon"]); d=polygon.distance(Point(center(c["bbox_pt"])))
                    if d>85:
                        continue
                    leader=leader_support(c,polygon,strokes)
                    pc.append({"contour_id":contour["id"],"score":d-(35 if leader else 0),"leader_primitive_ids":leader})
                pc.sort(key=lambda q:q["score"])
                part.update(candidate_contours=pc[:5],contour_id=None,polygon=None,member_ids=[],
                            status="boundary_unresolved")
                # Closed cells in a lattice are not plates. Only display a plate
                # boundary automatically when its circle has a touching leader.
                detail_contour = view["role"]=="detail" and len(pc)==1
                if pc and (pc[0]["leader_primitive_ids"] or detail_contour):
                    best=pc[0];contour=next(q for q in contours if q["id"]==best["contour_id"])
                    part.update(contour_id=contour["id"],polygon=contour["polygon"],status="detail_contour_candidate" if detail_contour else "leader_supported_contour_candidate")
                    part["primitive_ids"]=sorted(set(part["primitive_ids"]+contour["primitive_ids"]+best["leader_primitive_ids"]))
                plates.append(part)
    return members,plates,unknown


def link_features(members, plates, holes):
    """Containment is a projected ownership candidate, not a drilled part count."""
    for part in members+plates:
        if not part.get("polygon"):
            continue
        polygon=Polygon(part["polygon"]).buffer(.4)
        for hole in holes:
            if part["view_id"]==hole["view_id"] and polygon.covers(Point(hole["center_pt"])):
                part["hole_ids"].append(hole["id"])
                hole["member_ids" if part in members else "plate_ids"].append(part["id"])
    for hole in holes:
        n=len(hole["member_ids"])+len(hole["plate_ids"])
        hole["owner_status"]="multiple_projected_owners" if n>1 else "projected_owner_candidate" if n else "unresolved"
    for plate in plates:
        plate["member_ids"]=sorted({m["id"] for m in members if m["view_id"]==plate["view_id"]
                                     and set(m["hole_ids"])&set(plate["hole_ids"])
                                     and not (m.get("side") and plate.get("side") and m["side"]!=plate["side"])})


def check_bom_length_consistency(members):
    """Flag incomplete/misassociated axes using a robust within-view length ratio.

    This is a diagnostic only: cut lengths and projected node distances are not
    identical. It neither calibrates engineering geometry nor forces a match.
    """
    records=[];seen=set()
    for m in members:
        if not m["axis"]:continue
        length=math.dist(m["axis"]["a"],m["axis"]["b"])
        m["observed_axis_length_pt"]=length
        if length<=0:continue
        m["cut_length_to_projection_ratio"]=m["bom"]["length_mm"]/length
        if m["projection_group"] not in seen:
            records.append(m);seen.add(m["projection_group"])
    if len(records)<5:return None
    groups=[[m for m in records if abs(math.log(m["cut_length_to_projection_ratio"]/seed["cut_length_to_projection_ratio"]))<.12] for seed in records]
    best=max(groups,key=len)
    if len(best)<max(4,len(records)*.5):return None
    ratio=float(np.median([m["cut_length_to_projection_ratio"] for m in best]))
    for m in members:
        if "cut_length_to_projection_ratio" not in m:continue
        deviation=m["cut_length_to_projection_ratio"]/ratio-1
        m["bom_projection_check"]={"ratio_deviation":deviation,"status":"possible_partial_or_wrong_axis" if abs(deviation)>.18 else "within_candidate_view_ratio", "verified_dimension":False}
    return {"median_cut_length_to_projection_ratio":ratio,"supporting_member_ids":[m["id"] for m in best],
            "status":"diagnostic_only_not_a_mm_coordinate_transform"}
