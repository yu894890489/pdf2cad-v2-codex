"""Explicit, reversible assembly geometry edits inferred from neighboring faces.

Native PDF/BOM observations remain immutable. Corrections are stored in the scene
and applied to freshly extracted faces, never accumulated from previous edits.
Fitting a boundary is not independent dimensional verification.
"""
import copy
import math

import numpy as np
from shapely.geometry import Polygon

from .assembly import TOLERANCE_MM, matrix_pose, transformed, link_residual
from .assembly_fitting import _link
from .io import digest


def map_point(matrix, point):
    q=np.asarray(matrix)@np.array([point[0],point[1],1.])
    if abs(q[2])<1e-8:raise ValueError('轮廓映射在当前图元处退化，无法生成修正模型。')
    result=[float(q[0]/q[2]),float(q[1]/q[2]),0.]
    if not np.isfinite(result).all() or max(abs(v) for v in result)>1e8:
        raise ValueError('轮廓映射超出合理范围，请核对公共边端点。')
    return result


def _homography(source,target):
    # Normalize coordinates for large drawings before solving eight parameters.
    def normalize(points):
        xy=np.asarray(points)[:,:2];c=xy.mean(axis=0);scale=max(np.ptp(xy,axis=0))
        if scale<1e-6:raise ValueError('公共边不能确定有效的二维轮廓。')
        t=np.array([[1/scale,0,-c[0]/scale],[0,1/scale,-c[1]/scale],[0,0,1.]])
        return (xy-c)/scale,t
    source_xy,ts=normalize(source);target_xy,tt=normalize(target)
    for points in (source_xy,target_xy):
        poly=Polygon(points)
        if not poly.is_valid or poly.area<1e-5:
            raise ValueError('公共边形成交叉或退化轮廓，请先核对端点方向。')
    a=[];b=[]
    for (x,y),(u,v) in zip(source_xy,target_xy):
        a.extend([[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]]);b.extend([u,v])
    if np.linalg.cond(a)>1e9:raise ValueError('轮廓约束接近退化，无法稳定修正。')
    h=np.append(np.linalg.solve(a,b),1).reshape(3,3)
    h=np.linalg.inv(tt)@h@ts
    # All source corners must stay on the same side of the projective horizon.
    weights=[(h@np.array([p[0],p[1],1]))[2] for p in source]
    if min(weights)*max(weights)<=0:raise ValueError('轮廓映射发生翻折，不能直接修正。')
    return h/h[2,2]


def split_length_change(a,b,observed_midpoint,original_length):
    """Keep physical section size and expose added/removed axial material.

    The original length is centered at its mapped observation midpoint on the
    revised axis. This explicitly separates length edits from layout relocation.
    Retained + removed = original length; retained + added = corrected length.
    """
    a,b,mid=np.asarray(a),np.asarray(b),np.asarray(observed_midpoint)
    length=float(np.linalg.norm(b-a));u=(b-a)/length
    center=float((mid-a)@u);lo=center-original_length/2;hi=center+original_length/2
    retained_start=max(0.,lo);retained_end=min(length,hi)
    segments=[]
    def add(kind,start,end):
        if end-start>1e-6:segments.append({'kind':kind,'a':(a+u*start).tolist(),'b':(a+u*end).tolist(),'length_mm':end-start})
    if retained_end>=retained_start:
        add('added',0,retained_start);add('retained',retained_start,retained_end);add('added',retained_end,length)
    else:add('added',0,length)
    add('trimmed',lo,min(hi,0.));add('trimmed',max(lo,length),hi)
    return segments


def _span_between_sides(a,b,sides):
    a,b=np.asarray(a),np.asarray(b);u=b[:2]-a[:2];hits=[]
    for start,end in sides:
        start,end=np.asarray(start),np.asarray(end);v=end[:2]-start[:2]
        matrix=np.column_stack((u,-v))
        if abs(np.linalg.det(matrix))<1e-6:return None
        t,s=np.linalg.solve(matrix,start[:2]-a[:2])
        if not -.005<=s<=1.005:return None
        hits.append(float(t))
    lo,hi=sorted(hits)
    # Only extend the same diagonal to both bounding chords, not to arbitrary
    # remote intersections of construction lines.
    if lo>.05 or hi<.95 or hi-lo>2.5:return None
    return [a+(b-a)*lo,a+(b-a)*hi]


def build_face_correction(face,report,input_digest):
    if not face.get('members'):raise ValueError('该面尚无可修正的杆件模型，请先完成杆件识别。')
    p=report['prediction']
    if p['planarity_max_mm']>TOLERANCE_MM:
        raise ValueError(f"邻面端点离平面最大 {p['planarity_max_mm']:.1f} mm，超过 3 mm；先调整邻面再生成平面修正模型。")
    world=np.asarray(p['planar_corners_world_mm']);origin=world.mean(axis=0)
    z=np.asarray(p['plane_normal']);x=world[1]-world[0];x/=np.linalg.norm(x)
    y=np.cross(z,x);y/=np.linalg.norm(y);x=np.cross(y,z)
    frame=np.eye(4);frame[:3,:3]=np.column_stack((x,y,z));frame[:3,3]=origin
    local=(world-origin)@frame[:3,:3];local[:,2]=0
    fitting=report['native_candidate_fit'] or report['original_fit']
    axes=report['native_candidate_axes_local']
    ordered=[np.asarray(axis)[::-1] if flip else np.asarray(axis) for axis,flip in zip(axes,fitting['endpoint_flips'])]
    source=np.array([ordered[0][0],ordered[0][1],ordered[1][1],ordered[1][0]])
    h=_homography(source,local)
    anchors={row['member_id']:i for i,row in enumerate(report['comparisons'])}
    corrected=[]
    sides=[[local[0],local[1]],[local[3],local[2]]]
    for member in face['members']:
        old=np.array([member['a'],member['b']]);old_length=math.dist(*old)
        mapped=np.array([map_point(h,point) for point in old]);endpoints=mapped.copy()
        basis='outline_layout_adjustment'
        if member['id'] in anchors:
            i=anchors[member['id']];endpoints=np.array(sides[i])
            if fitting['endpoint_flips'][i]:endpoints=endpoints[::-1]
            basis='shared_mark_neighbor_endpoints'
        elif member.get('bom_length_mm') and old_length<member['bom_length_mm']*.95:
            span=_span_between_sides(*mapped,sides)
            if span is not None and abs(math.dist(*span)-member['bom_length_mm'])<=member['bom_length_mm']*.15:
                endpoints=np.array(span);basis='diagonal_axis_intersections_with_bounding_chords'
        length=math.dist(*endpoints)
        if length<1e-6:raise ValueError('修正后出现零长度杆件，请检查件号对应。')
        segments=split_length_change(*endpoints,mapped.mean(axis=0),old_length)
        corrected.append({'member_id':member['id'],'mark':member['mark'],'a':endpoints[0].tolist(),'b':endpoints[1].tolist(),
                          'original_axis_local':old.tolist(),'mapped_observed_axis_local':mapped.tolist(),
                          'original_length_mm':old_length,'corrected_length_mm':length,
                          'bom_length_mm':member.get('bom_length_mm'),
                          'bom_difference_mm':length-member['bom_length_mm'] if member.get('bom_length_mm') else None,
                          'added_mm':sum(s['length_mm'] for s in segments if s['kind']=='added'),
                          'trimmed_mm':sum(s['length_mm'] for s in segments if s['kind']=='trimmed'),
                          'segments':segments,'basis':basis,'endpoints_verified':False})
    summary={'status':'applied_candidate','member_count':len(corrected),
             'extended_members':sum(m['added_mm']>TOLERANCE_MM for m in corrected),
             'trimmed_members':sum(m['trimmed_mm']>TOLERANCE_MM for m in corrected),
             'bom_conflicts':sum(abs(m['bom_difference_mm'] or 0)>TOLERANCE_MM for m in corrected),
             'engineering_verified':False}
    return {'schema_version':'1.0','face_id':face['id'],'input_digest':input_digest,
            'source_face_digest':digest(face),'mapping':h.tolist(),'pose':matrix_pose(frame),
            'outline_local':local.tolist(),'members':corrected,'summary':summary,
            'basis':'neighbor_outline_fit_with_explicit_length_edits',
            'length_color_basis':'original_length_centered_on_mapped_observation_along_corrected_axis',
            'reference_edge_ids':p['reference_edge_ids'],'engineering_verified':False}


def apply_face_correction(face,correction,input_digest):
    if correction['input_digest']!=input_digest or correction['source_face_digest']!=digest(face):
        raise ValueError('杆件修正的原始几何或比例已变化，请重新反算。')
    result=copy.deepcopy(face);h=correction['mapping'];updates={m['member_id']:m for m in correction['members']}
    for member in result['members']:
        update=updates[member['id']];member.update(a=update['a'],b=update['b'],endpoints_verified=False,correction=copy.deepcopy(update))
    for plate in result.get('plates',[]):
        if plate.get('polygon'):plate['polygon']=[map_point(h,p) for p in plate['polygon']]
        for hole in plate.get('drill_holes_local',[]):hole['center']=map_point(h,hole['center'])
    for hole in result.get('holes',[]):hole['center']=map_point(h,hole['center'])
    result['polygon']=[map_point(h,p) for p in face['polygon']]
    result['wire']=[[map_point(h,p) for p in line] for line in face['wire']]
    for edge in result['edges']:
        update=updates.get(edge.get('member_id'))
        edge['a'],edge['b']=(update['a'],update['b']) if update else (map_point(h,edge['a']),map_point(h,edge['b']))
        edge['length']=math.dist(edge['a'],edge['b']);edge['endpoints_verified']=False
        if update:edge['basis']='inferred_corrected_member_axis'
    result['correction']=copy.deepcopy(correction['summary'])
    result['correction']['outline_local']=correction['outline_local']
    return result


def place_correction(faces,scene,face,report,input_digest):
    correction=build_face_correction(face,report,input_digest)
    updated=apply_face_correction(face,correction,input_digest)
    result=copy.deepcopy(scene);result['poses'][face['id']]=correction['pose']
    result.setdefault('face_corrections',{})[face['id']]=correction
    result['active_face_ids']=sorted(set(result['active_face_ids'])|{face['id']})
    lookup={e['id']:e for f in faces if f['id']!=face['id'] for e in f['edges']}
    lookup.update({e['id']:e for e in updated['edges']})
    links=[]
    for row in report['comparisons']:
        a,b=lookup[row['reference_edge_id']],lookup[row['target_edge_id']]
        occupied=[l for l in result['links'] if {l['a'],l['b']}&{a['id'],b['id']}]
        if any({l['a'],l['b']}!={a['id'],b['id']} for l in occupied):
            raise ValueError('反算使用的公共杆件已有其他连接，请先检查该边对应关系。')
        link=_link(a,b,result['poses']);link['basis']='inferred_face_correction'
        result['links']=[l for l in result['links'] if l not in occupied];result['links'].append(link)
        links.append({**link,**link_residual(link,lookup,result['poses'])})
    report['correction']={**correction['summary'],'members':copy.deepcopy(correction['members']),
                          'max_endpoint_gap_mm':max(l['endpoint_gap_mm'] for l in links),
                          'links':links,'pose':correction['pose'],'outline_local':correction['outline_local'],
                          'source_geometry_preserved':True,'corrected_geometry_is_inferred':True}
    report['assembly_geometry_modified']=True
    report['issues'].append('已按邻面轮廓修改整面杆件几何并放置，补长/截短为推断修正；对齐间隙变小不等于独立验证尺寸，BOM 差异仍须核对。')
    return result,updated


def validate_corrections(scene,faces):
    corrections=scene.get('face_corrections',{})
    lookup={f['id']:f for f in faces}
    if not isinstance(corrections,dict) or not set(corrections)<=set(lookup):raise ValueError('修正模型引用的结构面无效。')
    for fid,correction in corrections.items():
        if not isinstance(correction,dict) or correction.get('face_id')!=fid:raise ValueError('修正模型记录无效。')
        if not all(isinstance(correction.get(k),str) and correction[k] for k in ('input_digest','source_face_digest')):
            raise ValueError('修正模型缺少原始几何校验信息。')
        summary=correction.get('summary')
        if not isinstance(summary,dict) or summary.get('status')!='applied_candidate' or summary.get('engineering_verified') is not False:
            raise ValueError('修正模型必须标为待核对候选。')
        members=correction.get('members',[])
        expected={m['id'] for m in lookup[fid]['members']}
        if not isinstance(members,list) or any(not isinstance(m,dict) for m in members) or len(members)!=len(expected) or {m.get('member_id') for m in members}!=expected:raise ValueError('修正杆件与原始结构面不一致。')
        try:
            matrix=np.asarray(correction.get('mapping'),dtype=float)
            outline=np.asarray(correction.get('outline_local'),dtype=float)
        except (TypeError,ValueError) as exc:raise ValueError('修正轮廓映射无效。') from exc
        if matrix.shape!=(3,3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-15:raise ValueError('修正轮廓映射无效。')
        if outline.shape!=(4,3) or not np.isfinite(outline).all():raise ValueError('修正面轮廓无效。')
        for member in members:
            segments=member.get('segments')
            if not isinstance(segments,list) or not segments or any(not isinstance(s,dict) or s.get('kind') not in {'retained','added','trimmed'} for s in segments):
                raise ValueError('修正杆件分段无效。')
            for segment in [member,*segments]:
                for key in ('a','b'):
                    try:point=np.asarray(segment.get(key),dtype=float)
                    except (TypeError,ValueError) as exc:raise ValueError('修正杆件端点无效。') from exc
                    if point.shape!=(3,) or not np.isfinite(point).all() or max(abs(point))>1e8:
                        raise ValueError('修正杆件端点无效。')
            for key in ('original_length_mm','corrected_length_mm','added_mm','trimmed_mm'):
                value=member.get(key)
                if not isinstance(value,(float,int)) or not math.isfinite(value) or not 0<=value<=1e8:raise ValueError('修正杆件长度无效。')
            lengths={kind:sum(math.dist(s['a'],s['b']) for s in segments if s['kind']==kind) for kind in ('retained','added','trimmed')}
            if (abs(lengths['retained']+lengths['added']-member['corrected_length_mm'])>.001 or
                abs(lengths['retained']+lengths['trimmed']-member['original_length_mm'])>.001 or
                abs(lengths['added']-member['added_mm'])>.001 or abs(lengths['trimmed']-member['trimmed_mm'])>.001 or
                abs(math.dist(member['a'],member['b'])-member['corrected_length_mm'])>.001):
                raise ValueError('修正分段与杆件总长不一致。')


def write_corrected_svg(report,destination):
    from html import escape
    correction=report.get('correction',{})
    if correction.get('status')!='applied_candidate':return
    points=np.array([s[k] for m in correction['members'] for s in m['segments'] for k in ('a','b')])
    lo=points.min(axis=0);hi=points.max(axis=0);pad=max(hi-lo)*.04
    w,h=hi[0]-lo[0]+2*pad,hi[1]-lo[1]+2*pad
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{lo[0]-pad} {-hi[1]-pad} {w} {h}">',
         '<title>反算修正结构图：黄色保留，橙色补长，红色虚线截去；尺寸仍待核对</title>',
         f'<rect x="{lo[0]-pad}" y="{-hi[1]-pad}" width="{w}" height="{h}" fill="white"/>']
    for m in correction['members']:
        for segment in m['segments']:
            a,b=segment['a'],segment['b'];color={'retained':'#bea000','added':'#f16b13','trimmed':'#d82948'}[segment['kind']]
            dash=' stroke-dasharray="35 25"' if segment['kind']=='trimmed' else ''
            svg.append(f'<line x1="{a[0]}" y1="{-a[1]}" x2="{b[0]}" y2="{-b[1]}" stroke="{color}" stroke-width="18"{dash}/>')
        mid=(np.asarray(m['a'])+m['b'])/2
        svg.append(f'<text x="{mid[0]}" y="{-mid[1]-30}" font-size="65" fill="#374151" stroke="white" stroke-width="8" paint-order="stroke">{escape(str(m["mark"]))}</text>')
    svg.append('</svg>');(destination/'missing-face-corrected.svg').write_text('\n'.join(svg),encoding='utf8')
