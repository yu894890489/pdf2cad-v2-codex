"""Geometry supporting planar observations, never inferred 3D connections."""
import math
from collections import defaultdict

import numpy as np
from shapely.geometry import LineString, Point


def unit_segment(a, b):
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    length = float(np.linalg.norm(b-a))
    if length < 1e-8:
        return None
    u = (b-a)/length
    if u[0] < -1e-8 or (abs(u[0]) < 1e-8 and u[1] < 0):
        a, b, u = b, a, -u
    return a, b, u, length


def point_segment_distance(p, a, b):
    p, a, b = np.array(p), np.array(a), np.array(b)
    d = b-a
    t = float((p-a)@d/max(d@d, 1e-12))
    return float(np.linalg.norm(p-(a+np.clip(t, 0, 1)*d))), t


def native_strokes(primitives):
    """Turn thin painted quadrilaterals into stroke centrelines and retain provenance.

    These are ink-stroke centrelines, not member axes. Filling/stroking the same
    strip is deduplicated only in this derived layer; input records stay intact.
    """
    strokes, lookup = [], {}
    for p in primitives:
        if p.get('excluded_from_structure') or p.get("role") in {"dash_segment_candidate", "native_dashed", "sheet_frame_candidate"} or p.get("line_style") == "dashed":
            continue
        segments = []
        kind = p["primitive_type"]
        if kind == "line":
            segments = [(p["geometry"]["start"], p["geometry"]["end"], p["style"].get("width") or 0)]
        elif kind in {"quadrilateral", "polygon"}:
            points = p["geometry"].get("points", [])
            if len(points) > 1 and np.linalg.norm(np.array(points[0])-points[-1]) < .01:
                points = points[:-1]
            if len(points) == 4:
                sides = [(np.linalg.norm(np.array(points[(i+1)%4])-points[i]), i) for i in range(4)]
                short, i = min(sides)
                long = max(s[0] for s in sides)
                if short < 1.5 and long > short*5:
                    a = (np.array(points[i])+points[(i+1)%4])/2
                    b = (np.array(points[(i+2)%4])+points[(i+3)%4])/2
                    segments = [(a, b, short)]
            # Wider closed polygons are outlines, not broad ink strokes.
            if not segments and len(points) > 2:
                segments = [(points[i], points[(i+1)%len(points)], p.get("style", {}).get("width") or .5) for i in range(len(points))]
        elif kind in {"polyline", "rectangle"}:
            if kind == "rectangle":
                x0,y0,x1,y1 = p["geometry"]["rect"]
                if min(x1-x0,y1-y0) < .2:
                    continue
                points = [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]
            else:
                points = p["geometry"]["points"]
            segments = [(a,b,p.get("style",{}).get("width") or .5) for a,b in zip(points,points[1:])]
        for a, b, width in segments:
            info = unit_segment(a, b)
            if not info or info[3] < 1.2:
                continue
            a,b,u,length = info
            key = tuple(np.round(np.r_[a,b]/.08).astype(int))
            if key in lookup:
                strokes[lookup[key]]["primitive_ids"].append(p["id"])
                continue
            lookup[key] = len(strokes)
            strokes.append({"id": f"stroke_{len(strokes)+1:05d}", "a": a.tolist(), "b": b.tolist(),
                            "width_pt": float(width), "length_pt": length, "primitive_ids": [p["id"]]})
    return strokes


def merge_collinear(strokes, gap=5., tolerance=.5):
    """Join overlapping/short interrupted outlines, preserving gaps as evidence."""
    ordered = sorted(strokes, key=lambda s: s["length_pt"], reverse=True)
    tracks = []
    for stroke in ordered:
        a,b,u,length = unit_segment(stroke["a"], stroke["b"])
        found = None
        for track in tracks:
            v = np.array(track["u"])
            if abs(float(u@v)) < math.cos(math.radians(.35)):
                continue
            origin = np.array(track["origin"])
            n = np.array([-v[1],v[0]])
            if max(abs(float((a-origin)@n)), abs(float((b-origin)@n))) > tolerance:
                continue
            start,end = sorted([float((a-origin)@v),float((b-origin)@v)])
            if start > track["end"]+gap or end < track["start"]-gap:
                continue
            found = track
            track["start"] = min(track["start"], start)
            track["end"] = max(track["end"], end)
            track["intervals"].append([start,end])
            break
        if found is None:
            found = {"u":u.tolist(),"origin":a.tolist(),"start":0.,"end":length,"intervals":[[0.,length]],
                     "primitive_ids":[],"stroke_ids":[],"width_pt":stroke["width_pt"]}
            tracks.append(found)
        found["primitive_ids"].extend(stroke["primitive_ids"])
        found["stroke_ids"].append(stroke["id"])
        found["width_pt"] = max(found["width_pt"],stroke["width_pt"])
    for i,t in enumerate(tracks):
        origin,u = np.array(t["origin"]),np.array(t["u"])
        t.update(id=f"outline_{i+1:04d}",a=(origin+u*t["start"]).tolist(),b=(origin+u*t["end"]).tolist(),length_pt=t["end"]-t["start"])
        merged=[]
        for start,end in sorted(t.pop("intervals")):
            if merged and start<=merged[-1][1]:
                merged[-1][1]=max(end,merged[-1][1])
            else:
                merged.append([start,end])
        t["gaps_pt"] = [[merged[j-1][1],merged[j][0]] for j in range(1,len(merged))]
        t["primitive_ids"] = sorted(set(t["primitive_ids"]))
    return tracks


def parallel_bands(tracks, min_length=22., max_width=20.):
    """Propose a strip bounded by two native parallel outlines."""
    bands=[]
    for i,first in enumerate(tracks):
        if first["length_pt"] < min_length:
            continue
        a,b,u,_ = unit_segment(first["a"],first["b"])
        n=np.array([-u[1],u[0]])
        for second in tracks[i+1:]:
            if second["length_pt"] < min_length:
                continue
            c,d,v,_ = unit_segment(second["a"],second["b"])
            if abs(float(u@v)) < math.cos(math.radians(.7)):
                continue
            offsets=[float((c-a)@n),float((d-a)@n)]
            if abs(offsets[0]-offsets[1])>1.2:
                continue
            offset=sum(offsets)/2
            width=abs(offset)
            if not 1.2 <= width <= max_width:
                continue
            c0,c1=sorted([float((c-a)@u),float((d-a)@u)])
            lo,hi=max(0,c0),min(first["length_pt"],c1)
            length=hi-lo
            if length<min_length or length<.5*min(first["length_pt"],second["length_pt"]):
                continue
            start,end=a+u*lo+n*offset/2,a+u*hi+n*offset/2
            corners=[a+u*lo,a+u*hi,a+u*hi+n*offset,a+u*lo+n*offset]
            bands.append({"id":f"band_{len(bands)+1:04d}","a":start.tolist(),"b":end.tolist(),"width_pt":width,
                          "length_pt":length,"polygon":[q.tolist() for q in corners],"outline_ids":[first["id"],second["id"]],
                          "primitive_ids":sorted(set(first["primitive_ids"]+second["primitive_ids"]))})
    return bands


def extend_supported_bands(bands, tracks):
    """Bridge interrupted second edges only along an observed continuous backbone.

    A long chord can have a continuous lower edge and upper edges interrupted by
    joint plates. Require at least 55% opposing-edge coverage; save every gap.
    """
    output=list(bands)
    for track in tracks:
        if track["length_pt"] < 180:
            continue
        a,b,u,length=unit_segment(track["a"],track["b"]);n=np.array([-u[1],u[0]])
        groups=[]
        for band in bands:
            if track["id"] not in band["outline_ids"]:
                continue
            offset=float((np.array(band["a"])-a)@n)
            group=next((g for g in groups if abs(g["offset"]-offset)<.45),None)
            if group is None:
                group={"offset":offset,"bands":[]};groups.append(group)
            group["bands"].append(band)
        for group in groups:
            intervals=sorted(sorted([float((np.array(p)-a)@u) for p in (band["a"],band["b"])]) for band in group["bands"])
            merged=[]
            for lo,hi in intervals:
                if merged and lo<=merged[-1][1]: merged[-1][1]=max(hi,merged[-1][1])
                else: merged.append([lo,hi])
            coverage=sum(hi-lo for lo,hi in merged)/length
            if len(group["bands"])<2 or coverage<.55:
                continue
            offset=group["offset"]
            new={"id":f"band_{len(output)+1:04d}","a":(a+n*offset).tolist(),"b":(b+n*offset).tolist(),
                 "width_pt":abs(offset*2),"length_pt":length,
                 "polygon":[p.tolist() for p in (a,b,b+n*offset*2,a+n*offset*2)],
                 "outline_ids":sorted({pid for band in group["bands"] for pid in band["outline_ids"]}),
                 "primitive_ids":sorted({pid for band in group["bands"] for pid in band["primitive_ids"]}),
                 "supporting_band_ids":[band["id"] for band in group["bands"]],
                 "opposing_edge_coverage":coverage,"observed_opposing_intervals_pt":merged,
                 "basis":"continuous_native_backbone_with_interrupted_opposite_edge"}
            output.append(new)
    return output


def crossing(a,b,c,d):
    a,b,c,d=map(lambda p:np.array(p,dtype=float),(a,b,c,d))
    u,v=b-a,d-c
    determinant=float(u[0]*v[1]-u[1]*v[0])
    if abs(determinant)<1e-8:
        return None
    q=c-a
    t=float((q[0]*v[1]-q[1]*v[0])/determinant)
    s=float((q[0]*u[1]-q[1]*u[0])/determinant)
    return (a+t*u).tolist(),t,s


def classify_axis_contact(first,second,tolerance=8.):
    """A projected interior crossing is explicitly NOT a graph connection."""
    result=crossing(first["a"],first["b"],second["a"],second["b"])
    if result:
        point,t,s=result
        l1=math.dist(first["a"],first["b"]);l2=math.dist(second["a"],second["b"])
        if -tolerance/l1<=t<=1+tolerance/l1 and -tolerance/l2<=s<=1+tolerance/l2:
            near1=min(abs(t),abs(1-t))*l1<=tolerance
            near2=min(abs(s),abs(1-s))*l2<=tolerance
            return {"point":point,"kind":"endpoint_contact_candidate" if near1 and near2 else "tee_contact_candidate" if near1 or near2 else "crossing_without_connection_evidence",
                    "parameters":[t,s],"connected":False}
    pairs=[(math.dist(a,b),a,b) for a in (first["a"],first["b"]) for b in (second["a"],second["b"])]
    distance,a,b=min(pairs,key=lambda q:q[0])
    if distance<=tolerance:
        return {"point":((np.array(a)+b)/2).tolist(),"kind":"near_endpoints_candidate","gap_pt":distance,"connected":False}
    return None
