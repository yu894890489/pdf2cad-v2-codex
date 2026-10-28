"""Recover member extents hidden by joint plates without shortening BOM stock.

The parallel-band intersection is an observation window, not a cut end. Native
edge continuation and end cuts supply anchors. BOM length may constrain a second
end only with independent view-scale support and local joint evidence. Every
inferred interval remains distinguishable from ink actually visible in the PDF.
"""
import copy
import math
from collections import defaultdict

import numpy as np

from .topology_geometry import unit_segment, merge_collinear
from .topology_views import center


def view_scale(members):
    """A tight consensus of different observations, not one truncated member."""
    unique={}
    for m in members:
        if not m.get("axis") or not m["bom"]["length_mm"]:
            continue
        length=math.dist(m["axis"]["a"],m["axis"]["b"])
        if length>1:
            unique.setdefault(m["projection_group"],(m,m["bom"]["length_mm"]/length))
    values=list(unique.values())
    if len(values)<3:
        return None
    clusters=[[entry for entry in values if abs(math.log(entry[1]/seed[1]))<.025] for seed in values]
    best=max(clusters,key=len)
    if len(best)<max(3,len(values)*.4):
        return None
    ratio=float(np.median([r for _,r in best]))
    return {"mm_per_pt_candidate":ratio,"supporting_member_ids":[m["id"] for m,_ in best],
            "status":"independent_member_consensus_not_dimension_calibration"}


def hidden_outline_tracks(primitives,members=None):
    """Keep dense short-line chains as supplemental evidence, never new rods.

    Some CAD exports paint hidden outlines as sub-point line fragments. They
    are intentionally absent from the solid band detector. Only paired chains
    aligned with an existing member and containing a joint hole may extend it.
    """
    fragments=[]
    for p in primitives:
        if p.get('excluded_from_structure'):continue
        if p["primitive_type"]!="line" or p.get("role")=="sheet_frame_candidate":continue
        info=unit_segment(p["geometry"]["start"],p["geometry"]["end"])
        if not info or not .2<=info[3]<=1.2:continue
        a,b,_,length=info
        fragments.append({"id":p["id"],"a":a.tolist(),"b":b.tolist(),"length_pt":length,
                          "width_pt":p.get("style",{}).get("width") or 0,"primitive_ids":[p["id"]]})
    if members is not None and fragments:
        starts=np.array([s["a"] for s in fragments]);ends=np.array([s["b"] for s in fragments])
        keep=np.zeros(len(fragments),dtype=bool)
        for m in members:
            if not m.get("axis") or not m.get("polygon"):continue
            a,_,u,length=unit_segment(m["axis"]["a"],m["axis"]["b"])
            n=np.array([-u[1],u[0]]);width=math.dist(m["polygon"][0],m["polygon"][-1]);tol=min(.8,max(.3,width*.08))
            t0,t1=(starts-a)@u,(ends-a)@u;n0,n1=(starts-a)@n,(ends-a)@n
            near_end=((np.minimum(t0,t1)<.8)|(np.maximum(t0,t1)>length-.8))&(np.maximum(t0,t1)>-width*6)&(np.minimum(t0,t1)<length+width*6)
            keep|=near_end&(np.abs(n0-n1)<.03)&(np.abs(np.abs(n0)-width/2)<tol)&(np.abs(np.abs(n1)-width/2)<tol)
        fragments=[s for s,k in zip(fragments,keep) if k]
    result=[];fragment_map={s["id"]:s for s in fragments}
    tracks=merge_collinear(fragments,gap=.65,tolerance=.12)
    # Length-ordered merging can leave two chains which only become adjacent
    # after a later fragment bridges them. Close these chains to a fixed point.
    while tracks:
        merged=merge_collinear(tracks,gap=.65,tolerance=.12)
        if len(merged)==len(tracks):break
        tracks=merged
    for track in tracks:
        if len(track["primitive_ids"])<3 or track["length_pt"]<3:continue
        a,_,u,_=unit_segment(track["a"],track["b"])
        intervals=sorted(sorted(float((np.array(fragment_map[pid][key])-a)@u) for key in ("a","b")) for pid in track["primitive_ids"])
        gaps=[];end=intervals[0][1]
        for lo,hi in intervals[1:]:
            if lo>end:gaps.append([end,lo])
            end=max(end,hi)
        track["gaps_pt"]=gaps
        track["stroke_ids"]=track["primitive_ids"][:]
        gaps=sum(b-a for a,b in track["gaps_pt"])
        if gaps/track["length_pt"]>.45:continue
        track.update(id="hidden_"+track["primitive_ids"][0],source_kind="paired_hidden_outline_evidence")
        result.append(track)
    return result


def _paired_hidden_edges(member,hidden,holes):
    if not member.get("axis") or not member.get("polygon"):return []
    a,b,u,length=unit_segment(member["axis"]["a"],member["axis"]["b"])
    n=np.array([-u[1],u[0]]);width=math.dist(member["polygon"][0],member["polygon"][-1])
    intervals,_=_intervals(hidden,a,u,n,width,length,width*6)
    accepted=set()
    for left in intervals:
        for right in intervals:
            if left["side"]==right["side"]:continue
            lo=max(left["interval"][0],right["interval"][0]);hi=min(left["interval"][1],right["interval"][1])
            if hi-lo<3 or lo>length+1.2 or hi<-1.2:continue
            end_a=lo<-.8 and abs(left["interval"][0]-right["interval"][0])<.8
            end_b=hi>length+.8 and abs(left["interval"][1]-right["interval"][1])<.8
            if not (end_a or end_b):continue
            if not _joint_evidence(lo,hi,a,u,n,width,[],holes)["hole_ids"]:continue
            accepted.update((left["stroke_id"],right["stroke_id"]))
    return [s for s in hidden if s["id"] in accepted]


def _intervals(strokes, a, u, n, width, length, reach):
    result=[];caps=[]
    for s in strokes:
        p,q=np.array(s["a"]),np.array(s["b"])
        t0,t1=float((p-a)@u),float((q-a)@u)
        n0,n1=float((p-a)@n),float((q-a)@n)
        lo,hi=sorted((t0,t1))
        if hi < -reach or lo > length+reach:
            continue
        tolerance=min(.8,max(.3,width*.08))
        side=1 if (n0+n1)>0 else -1
        if (abs(n1-n0)<=max(.2,s["length_pt"]*math.sin(math.radians(.6)))
                and max(abs(n0-side*width/2),abs(n1-side*width/2))<=tolerance):
            result.append({"interval":[lo,hi],"side":side,"primitive_ids":s["primitive_ids"],"stroke_id":s["id"],
                           "source_kind":s.get("source_kind","solid_native_edge")})
        # Small transverse segments can be a cut edge. Plate borders and other
        # crossing outlines remain alternatives until adjoining edge support is checked.
        if (min(n0,n1)<=-width*.4 and max(n0,n1)>=width*.4
                and s["length_pt"]<=width*3 and abs(n1-n0)>1e-8):
            t=t0+(t1-t0)*(-n0)/(n1-n0)
            caps.append({"position":t,"primitive_ids":s["primitive_ids"],"stroke_id":s["id"]})
    return result,caps


def _joint_evidence(lo,hi,a,u,n,width,plates,holes):
    hole_ids=[];plate_ids=[]
    for h in holes:
        p=np.array(h["center_pt"])-a
        if lo-.5<=float(p@u)<=hi+.5 and abs(float(p@n))<=width/2+.5:
            hole_ids.append(h["id"])
    for p in plates:
        c=np.array(center(p["callout_bbox_pt"]))-a
        radius=(p["callout_bbox_pt"][2]-p["callout_bbox_pt"][0])/2
        if lo-width*2<=float(c@u)<=hi+width*2 and abs(float(c@n))<max(35,width*3)+radius:
            plate_ids.append(p["id"])
    return {"hole_ids":hole_ids,"plate_ids":plate_ids}


def _connected_support(intervals,length,width,a,u,n,plates,holes):
    """Trace native matching edges across evidenced local joint gaps."""
    selected=[];lo,hi=0.,length
    pending=list(intervals)
    while True:
        changed=False
        for item in pending[:]:
            start,end=item["interval"]
            gap=max(0,lo-end,start-hi)
            if gap>max(12,width*5):
                continue
            if gap>2:
                g0,g1=(end,lo) if end<lo else (hi,start)
                evidence=_joint_evidence(g0,g1,a,u,n,width,plates,holes)
                if not evidence["hole_ids"] and not evidence["plate_ids"]:
                    continue
            selected.append(item);pending.remove(item)
            lo=min(lo,start);hi=max(hi,end);changed=True
        if not changed:
            return selected,lo,hi


def _conflicting_members(member,lo,hi,a,u,n,width,others):
    conflicts=[]
    for other in others:
        if (not other.get("axis") or other["id"]==member["id"] or other["projection_group"]==member["projection_group"]
                or (member.get("side") and other.get("side") and member["side"]!=other["side"])):
            continue
        p,q,v,ol=unit_segment(other["axis"]["a"],other["axis"]["b"])
        if abs(float(u@v))<math.cos(math.radians(.7)) or max(abs(float((p-a)@n)),abs(float((q-a)@n)))>max(.8,width*.25):
            continue
        start,end=sorted((float((p-a)@u),float((q-a)@u)))
        if min(hi,end)-max(lo,start)>1:
            conflicts.append(other["id"])
    return conflicts


def _coverage(lo,hi,support):
    merged=[]
    for start,end in sorted((max(lo,s["interval"][0]),min(hi,s["interval"][1])) for s in support):
        if end<=start:continue
        if merged and start<=merged[-1][1]+.3:merged[-1][1]=max(end,merged[-1][1])
        else:merged.append([start,end])
    gaps=[];position=lo
    for start,end in merged:
        if start-position>.5:gaps.append([position,start])
        position=max(position,end)
    if hi-position>.5:gaps.append([position,hi])
    return merged,gaps


def _end_hole_rows(boundary,direction,a,u,n,width,holes,reach):
    """Follow the nearest regular bolt rows beyond a visible endpoint.

    Stop at a pitch break: holes on the next member/plate are not an unlimited
    continuation of the current member. Row membership remains projected evidence.
    """
    rows=[]
    for h in holes:
        relative=np.array(h["center_pt"])-a
        t,offset=float(relative@u),float(relative@n)
        distance=(t-boundary)*direction
        if not .4<distance<=reach or abs(offset)>width/2+.2:continue
        row=next((r for r in rows if abs(r["offset"]-offset)<min(.8,max(.4,width*.05))),None)
        if row is None:row={"offset":offset,"items":[]};rows.append(row)
        row["items"].append({"id":h["id"],"t":t,"distance":distance,"offset":offset,"radius":max(h["radii_pt"])})
    accepted=[]
    for row in rows:
        items=sorted(row["items"],key=lambda h:h["distance"])
        if items[0]["distance"]>max(25,width*3):continue
        picked=[items[0]];pitch=None
        for h in items[1:]:
            gap=h["distance"]-picked[-1]["distance"]
            if gap<.3:continue
            limit=min(max(12,width*1.8),pitch*1.35) if pitch else max(12,width*1.8)
            if gap>limit:break
            picked.append(h);pitch=gap if pitch is None else min(pitch,gap)
        accepted.extend(picked)
    return accepted


def native_extent_scale(members,geometry,plates,holes):
    """Estimate only from observed continuations, before using any BOM target.

    At least one unextended member must agree with the extended observations.
    A collection of truncated bands cannot calibrate itself by guessed lengths.
    """
    probes=copy.deepcopy(members);records={};unchanged=set()
    for m in probes:
        if not m.get("axis") or not m.get("polygon"):continue
        a,b,u,length=unit_segment(m["axis"]["a"],m["axis"]["b"])
        n=np.array([-u[1],u[0]]);width=math.dist(m["polygon"][0],m["polygon"][-1])
        strokes=geometry["strokes"]+_paired_hidden_edges(m,geometry.get("hidden_outlines",[]),holes)
        intervals,_=_intervals(strokes,a,u,n,width,length,width*6)
        support,lo,hi=_connected_support(intervals,length,width,a,u,n,plates,holes)
        if hi-lo>length+.8:
            gaps=_coverage(lo,0.,support)[1]+_coverage(length,hi,support)[1]
            if any(y-x>1.2 for x,y in gaps):continue
            if _conflicting_members(m,lo,0.,a,u,n,width,members) or _conflicting_members(m,length,hi,a,u,n,width,members):continue
            m["axis"]={"a":(a+u*lo).tolist(),"b":(a+u*hi).tolist()}
            records[m["id"]]={"axis":m["axis"],"primitive_ids":sorted({p for s in support for p in s["primitive_ids"]})}
        else:unchanged.add(m["id"])
    scale=view_scale(probes)
    if not scale or not unchanged.intersection(scale["supporting_member_ids"]):return None
    if not set(records).intersection(scale["supporting_member_ids"]):return None
    scale.update(status="native_extent_consensus_with_unextended_member_check",
                 native_extent_evidence={mid:records[mid] for mid in scale["supporting_member_ids"] if mid in records})
    return scale


def recover_member(member, strokes, plates, holes, others, scale, hidden=()):
    if not member.get("axis") or not member.get("polygon"):
        return {"status":"no_axis", "applied":False}
    a,b,u,length=unit_segment(member["axis"]["a"],member["axis"]["b"])
    n=np.array([-u[1],u[0]])
    width=math.dist(member["polygon"][0],member["polygon"][-1])
    paired_hidden=_paired_hidden_edges(member,hidden,holes)
    strokes=list(strokes)+paired_hidden
    target=member["bom"]["length_mm"]/scale["mm_per_pt_candidate"] if scale else None
    result={"status":"extent_unresolved","applied":False,"visible_axis":copy.deepcopy(member["axis"]),
            "visible_length_pt":length,"bom_cut_length_mm":member["bom"]["length_mm"],
            "target_length_pt_candidate":target,"scale_evidence":scale,"extensions":[],
            "alternatives":[],"full_endpoints_verified":False}
    # A complete-looking cut-length match is not a reason to extend every node.
    native_conflict=bool(target and target<length*.97 and paired_hidden)
    if target and target<=length+max(.8,length*.004) and not native_conflict:
        result["status"]="already_matches_bom_candidate" if abs(target-length)<length*.03 else "observed_span_exceeds_bom_candidate"
        return result
    reach=min(450,max(width*6,(target-length)*1.15 if target else 0))
    intervals,caps=_intervals(strokes,a,u,n,width,length,reach)
    support,lo,hi=_connected_support(intervals,length,width,a,u,n,plates,holes)
    solid_lo,solid_hi=lo,hi
    if paired_hidden:
        _,solid_lo,solid_hi=_connected_support([s for s in intervals if s["source_kind"]=="solid_native_edge"],length,width,a,u,n,plates,holes)
    # Do not use a drawing continuation past the maximum possible BOM span.
    hole_constraint=None
    if target and not native_conflict:
        # A plate edge can be collinear with the rod edge. If its apparent union
        # already exceeds the stock length, do not call that whole edge a member.
        # Start the pitch sequence at the solid body. A short hidden extension
        # must not skip the first bolt and mistake the next member's hole for
        # a second bolt in a new row with a different pitch.
        body_lo,body_hi=(solid_lo,solid_hi) if solid_hi-solid_lo<target*.98 else (0.,length)
        left_holes=_end_hole_rows(body_lo,-1,a,u,n,width,holes,reach)
        right_holes=_end_hole_rows(body_hi,1,a,u,n,width,holes,reach)
        if not left_holes:left_holes=_end_hole_rows(0.,-1,a,u,n,width,holes,reach)
        if not right_holes:right_holes=_end_hole_rows(length,1,a,u,n,width,holes,reach)
        # A complete observed edge span that matches the stock excludes holes
        # beyond its cut ends, even if an adjoining member has a parallel row.
        _,native_gaps=_coverage(lo,hi,support)
        if abs((hi-lo)/target-1)<.015 and all(y-x<=1.2 for x,y in native_gaps):
            excluded=[h["id"] for h in left_holes+right_holes if not lo-.5<=h["t"]<=hi+.5]
            left_holes=[h for h in left_holes if lo-.5<=h["t"]<=hi+.5]
            right_holes=[h for h in right_holes if lo-.5<=h["t"]<=hi+.5]
            if excluded:result["excluded_neighbor_hole_ids"]=excluded
        # The member's bolt gauge must continue along the same lateral row at
        # both ends. A diagonal member's hole at the joint is not another row on
        # this member merely because it falls in the same projected strip.
        tolerance=min(.8,max(.4,width*.05))
        left_matched=[h for h in left_holes if any(abs(h["offset"]-other["offset"])<tolerance for other in right_holes)]
        right_matched=[h for h in right_holes if any(abs(h["offset"]-other["offset"])<tolerance for other in left_holes)]
        left_holes,right_holes=left_matched,right_matched
        if left_holes and right_holes:
            lower=max(length-target,max(h["t"]+h["radius"] for h in right_holes)-target)
            upper=min(0.,min(h["t"]-h["radius"] for h in left_holes))
            hole_constraint={"start_interval_pt":[lower,upper],"end_interval_pt":[lower+target,upper+target],
                             "hole_ids":[h["id"] for h in left_holes+right_holes],
                             "status":"projected_bolt_rows_and_bom_not_verified_cut_edge"}
            result["hole_envelope_constraint"]=hole_constraint
            result["endpoint_ranges"]={"a":[(a+u*t).tolist() for t in (lower,upper)],
                                       "b":[(a+u*t).tolist() for t in (lower+target,upper+target)]}
            if lower>upper+.3:
                result["status"]="hole_span_conflicts_with_bom"
                return result
    evidence_ids=sorted({pid for item in support for pid in item["primitive_ids"]})
    candidates=[]
    def propose(start,end,basis,anchor_ids):
        if start>.1 or end<length-.1 or end-start<=length+.8:
            return
        if target and not native_conflict and (end-start)>target*1.015:
            return
        if hole_constraint:
            lower,upper=hole_constraint["start_interval_pt"]
            # Partial native recovery may be retained as an observation, but it
            # cannot serve as a complete member if it excludes the end bolt rows.
            if start<lower-.3 or start>upper+.3 or abs((end-start)-target)>.5:
                return
        extension_evidence=[]
        for left,right,end_name in ((start,0.,"a"),(length,end,"b")):
            if right-left<.5:
                continue
            evidence=_joint_evidence(left,right,a,u,n,width,plates,holes)
            supports=[s for s in support if min(right,s["interval"][1])-max(left,s["interval"][0])>.5]
            covered,gaps=_coverage(left,right,supports)
            if native_conflict:
                # A BOM conflict never permits length-based extrapolation. Both
                # hidden edges must actually cover this continuation and have a hole.
                if not evidence["hole_ids"]:return
                for side in (-1,1):
                    paired=[s for s in supports if s["side"]==side and s["source_kind"]=="paired_hidden_outline_evidence"]
                    if sum(y-x for x,y in _coverage(left,right,paired)[1])>1.2:return
            # BOM alone cannot assign the missing length to an arbitrary end.
            if not supports and not evidence["hole_ids"]:
                return
            # A short visible fragment cannot justify an arbitrarily long hidden
            # extension. Check every uncovered interval, including exterior ends.
            for g0,g1 in gaps:
                local=_joint_evidence(g0,g1,a,u,n,width,plates,holes)
                internal=any(c[1]<=g0+.5 for c in covered) and any(c[0]>=g1-.5 for c in covered)
                if g1-g0>1 and not local["hole_ids"] and not (internal and local["plate_ids"]):
                    return
            if _conflicting_members(member,left,right,a,u,n,width,others):
                return
            extension_evidence.append({"end":end_name,"interval_pt":[left,right],
                                       "a":(a+u*left).tolist(),"b":(a+u*right).tolist(),
                                       "length_pt":right-left,"native_edge_primitive_ids":sorted({pid for s in supports for pid in s["primitive_ids"]}),
                                       "hidden_edge_primitive_ids":sorted({pid for s in supports if s["source_kind"]=="paired_hidden_outline_evidence" for pid in s["primitive_ids"]}),
                                       "visible_edge_intervals_pt":covered,"inferred_intervals_pt":gaps,
                                       "hole_ids":evidence["hole_ids"],"plate_ids":evidence["plate_ids"],
                                       "evidence_status":"native_one_sided_edge_continuation" if supports else "bom_length_with_joint_hole_support"})
        mismatch=abs((end-start)/target-1) if target else 0
        inferred=sum(hi-lo for e in extension_evidence for lo,hi in e["inferred_intervals_pt"])
        score=mismatch*100 + inferred/max(width,1)*.1
        candidates.append({"start":start,"end":end,"basis":basis,"score":score,"anchor_primitive_ids":anchor_ids,
                           "extensions":extension_evidence})
    if hi-lo>length+.8:
        propose(lo,hi,"native_edge_union",evidence_ids)
    if target and not native_conflict:
        if hole_constraint:
            lower,upper=hole_constraint["start_interval_pt"]
            if upper-lower<=max(4,width*.45):
                position=(lower+upper)/2
                # Prefer a directly observed extent inside the feasible interval.
                for anchor in (lo,hi-target):
                    if lower<=anchor<=upper:position=anchor;break
                propose(position,position+target,"bom_and_end_hole_envelope",evidence_ids)
        # A continuous visible side supplies one end; a hole under the nearby
        # plate and the BOM constrain the other. Both possible anchors are kept.
        if lo<-.8:
            propose(lo,lo+target,"native_start_anchor_and_bom",evidence_ids)
        if hi>length+.8:
            propose(hi-target,hi,"native_end_anchor_and_bom",evidence_ids)
        for cap in caps:
            t=cap["position"]
            adjoining=[s for s in support if min(abs(t-s["interval"][0]),abs(t-s["interval"][1]))<1.2]
            if not adjoining:
                # A cut drawn inside a plate may have neither long edge visible.
                # It is useful only with a nearby hole projection on this strip.
                nearby=_joint_evidence(t-width,t+width,a,u,n,width,plates,holes)
                if not nearby["hole_ids"]:continue
            if -reach<=t<=.5:
                propose(t,t+target,"native_cut_anchor_and_bom",cap["primitive_ids"])
            elif length-.5<=t<=length+reach:
                propose(t-target,t,"native_cut_anchor_and_bom",cap["primitive_ids"])
    candidates.sort(key=lambda c:c["score"])
    result["alternatives"]=[{k:c[k] for k in ("start","end","basis","score","anchor_primitive_ids")} for c in candidates[:8]]
    if not candidates:
        if hole_constraint and hole_constraint["start_interval_pt"][1]-hole_constraint["start_interval_pt"][0]>max(4,width*.45):
            result["status"]="ambiguous_end_allocation"
        return result
    best=candidates[0]
    # Equally plausible placements >2pt apart must stay unresolved. The user can
    # inspect both; do not distribute missing length symmetrically by default.
    if any(abs(c["start"]-best["start"])>2 and c["score"]-best["score"]<.35 for c in candidates[1:]):
        result["status"]="ambiguous_end_allocation"
        return result
    if not target:
        result["status"]="native_extent_alternative_without_scale"
        return result
    result.update(status="overlap_recovered_candidate",applied=True,basis=best["basis"],extensions=best["extensions"],
                  axis={"a":(a+u*best["start"]).tolist(),"b":(a+u*best["end"]).tolist()},
                  recovered_length_pt=best["end"]-best["start"],
                  target_residual_pt=(best["end"]-best["start"]-target) if target else None,
                  primitive_ids=sorted(set(evidence_ids+best["anchor_primitive_ids"])))
    result["length_match_candidate"]=abs(result["target_residual_pt"])<=max(.8,target*.015)
    result["remaining_length_pt_candidate"]=max(0.,-result["target_residual_pt"])
    if not result["length_match_candidate"]:
        result["status"]="partial_overlap_recovered_candidate"
    if native_conflict:
        result.update(status="native_extent_bom_conflict_candidate",bom_conflict=True)
    start,end=np.array(result["axis"]["a"]),np.array(result["axis"]["b"])
    result["polygon"]=[p.tolist() for p in (start-n*width/2,end-n*width/2,end+n*width/2,start+n*width/2)]
    return result


def recover_overlaps(members,plates,holes,geometry):
    """Decisions use an immutable snapshot; earlier recoveries cannot bootstrap later ones."""
    originals=copy.deepcopy(members)
    for m in originals:
        if m.get("visible_axis"):
            m["axis"]=copy.deepcopy(m["visible_axis"])
            m["polygon"]=copy.deepcopy(m["visible_polygon"])
    groups=defaultdict(list)
    for m in originals:groups[m["view_id"]].append(m)
    scales={vid:view_scale(group) for vid,group in groups.items()}
    geometry_map={g["view_id"]:g for g in geometry}
    for vid,group in groups.items():
        if scales[vid]:continue
        scales[vid]=native_extent_scale(group,geometry_map[vid],[p for p in plates if p["view_id"]==vid],
                                       [h for h in holes if h["view_id"]==vid])
    # Borrow a page consensus only when at least two independent members in the
    # receiving view already agree. Edge-on sections are never scaled by title.
    page_scales=defaultdict(list)
    for vid,scale in scales.items():
        if scale:page_scales[groups[vid][0]["page"]].append((vid,scale))
    for vid,group in groups.items():
        if scales[vid]:continue
        peers=page_scales[group[0]["page"]]
        if len(peers)<3:continue
        cluster=max(([entry for entry in peers if abs(math.log(entry[1]["mm_per_pt_candidate"]/seed[1]["mm_per_pt_candidate"]))<.015] for seed in peers),key=len)
        if len(cluster)<3:continue
        ratio=float(np.median([s["mm_per_pt_candidate"] for _,s in cluster]));support=[];seen=set()
        for m in group:
            if not m.get("axis") or m["projection_group"] in seen:continue
            length=math.dist(m["axis"]["a"],m["axis"]["b"])
            if length>1 and abs(m["bom"]["length_mm"]/length/ratio-1)<.015:
                support.append(m["id"]);seen.add(m["projection_group"])
        if len(support)>=2:
            scales[vid]={"mm_per_pt_candidate":ratio,"supporting_member_ids":support,
                         "supporting_view_ids":[v for v,_ in cluster],"status":"page_consensus_with_two_local_member_checks"}
    original_map={m["id"]:m for m in originals}
    for member in members:
        vid=member["view_id"];source=original_map[member["id"]]
        result=recover_member(source,geometry_map[vid]["strokes"],[p for p in plates if p["view_id"]==vid],
                              [h for h in holes if h["view_id"]==vid],groups[vid],scales[vid],geometry_map[vid].get("hidden_outlines",[]))
        member["visible_axis"]=copy.deepcopy(source.get("axis"))
        member["visible_polygon"]=copy.deepcopy(source.get("polygon"))
        member["axis"]=copy.deepcopy(source.get("axis"))
        member["polygon"]=copy.deepcopy(source.get("polygon"))
        member["overlap_recovery"]=result
        if result["applied"]:
            member["axis"]=result["axis"];member["polygon"]=result["polygon"]
            member["primitive_ids"]=sorted(set(member["primitive_ids"]+result["primitive_ids"]))
    return {"version":"1.0","view_scales":scales,"records":[{"member_id":m["id"],"mark":m["mark"],"view_id":m["view_id"],**m["overlap_recovery"]} for m in members]}
