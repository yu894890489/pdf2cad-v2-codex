"""Leave-one-face-out prediction and independent native-vector diagnostics.

The assembled neighbors predict a boundary before the held-out drawing is read.
Neither the prediction nor a native outline proposal edits an input or certifies
an assembly. Rigid fits report residuals without stretching either observation.
"""
import itertools
import math

import numpy as np

from .assembly import TOLERANCE_MM, connected_component, matrix_pose, transformed
from .io import digest


def review_signature(input_digest, scene):
    return digest([input_digest, scene])


def _shared_pairs(faces, scene, target):
    edges={e['id']:e for f in faces for e in f['edges']}
    links=[link for link in scene['links'] if target['id'] not in {edges[link[k]]['face_id'] for k in ('a','b')}]
    root=scene['front_face_id']
    front=next((f for f in faces if f['id']==root),None)
    if not front or front.get('source_id')!=target.get('source_id'):
        root=scene.get('source_fronts',{}).get(target.get('source_id'),root)
    connected=connected_component(root,links,edges)-{target['id']}
    refs=[e for f in faces if f['id'] in connected and f['units']=='mm' and f.get('source_id')==target.get('source_id')
          for e in f['edges'] if e.get('mark') and e.get('bom_quantity')==1]
    pairs=[]
    for e in target['edges']:
        if not e.get('mark') or e.get('bom_quantity')!=1 or e.get('bom_length_mm') is None:continue
        same=[r for r in refs if r['mark']==e['mark'] and r['specification']==e['specification']
              and r.get('bom_length_mm') is not None and abs(r['bom_length_mm']-e['bom_length_mm'])<=TOLERANCE_MM]
        if len(same)==1:pairs.append((e,same[0]))
    return sorted(pairs,key=lambda pair:(-pair[0]['bom_length_mm'],pair[0]['id']))


def missing_face_candidates(faces,scene):
    result=[]
    for face in faces:
        if face['units']!='mm' or face.get('role') in {'schematic','detail'}:continue
        pairs=_shared_pairs(faces,scene,face)
        if len({r['face_id'] for _,r in pairs})<2:continue
        result.append({'face_id':face['id'],'title':face['title'],'shared_marks':[a['mark'] for a,_ in pairs],
                       'shared_member_count':len(pairs)})
    return sorted(result,key=lambda row:(-row['shared_member_count'],row['face_id']))


def missing_face_options(faces,scene):
    """Keep unavailable faces visible and explain what prevents a review."""
    options=[]
    for face in faces:
        if face.get('role') in {'schematic','detail'}:continue
        pairs=_shared_pairs(faces,scene,face) if face['units']=='mm' else []
        neighbor_count=len({ref['face_id'] for _,ref in pairs})
        ready=face['units']=='mm' and neighbor_count>=2
        reason=('可由已连接邻面反算。' if ready else '比例未定，请先确定该面的 mm/pt 比例。' if face['units']!='mm'
                else '尚未建立面间连接，请先尝试装配或连接公共杆件。' if not scene['links']
                else f'当前有 {neighbor_count} 个已连接邻面提供唯一公共杆件，反算至少需要 2 个。')
        options.append({'face_id':face['id'],'title':face.get('title',face['id']),'ready':ready,'reason':reason,
                        'shared_marks':[a['mark'] for a,_ in pairs],'shared_member_count':len(pairs),
                        'neighbor_count':neighbor_count})
    return sorted(options,key=lambda row:(not row['ready'],-row['shared_member_count'],row['face_id']))


def _rigid_fit(local,world):
    local,world=np.asarray(local),np.asarray(world)
    candidates=[]
    for flips in itertools.product((False,True),repeat=len(local)):
        x=np.concatenate([p[::-1] if flip else p for p,flip in zip(local,flips)])
        y=world.reshape(-1,3);xc=x.mean(axis=0);yc=y.mean(axis=0)
        u,_,vt=np.linalg.svd((x-xc).T@(y-yc))
        r=vt.T@u.T
        if np.linalg.det(r)<0:vt[-1]*=-1;r=vt.T@u.T
        matrix=np.eye(4);matrix[:3,:3]=r;matrix[:3,3]=yc-r@xc
        placed=x@r.T+matrix[:3,3];gaps=np.linalg.norm(placed-y,axis=1)
        candidates.append({'pose':matrix_pose(matrix),'endpoint_gaps_mm':gaps.tolist(),
                           'rms_gap_mm':float(np.sqrt(np.mean(gaps**2))),'max_gap_mm':float(max(gaps)),
                           'endpoint_flips':list(flips),'rigid_only':True})
    return min(candidates,key=lambda row:row['rms_gap_mm'])


def _merge(intervals):
    result=[]
    for start,end in sorted(intervals):
        if result and start<=result[-1][1]+.7:result[-1][1]=max(end,result[-1][1])
        else:result.append([float(start),float(end)])
    return result


def native_axis_evidence(member,geometry,ratio,bbox):
    """Locate the original two outline sides, independent of the assembled fit.

    Outermost native points give an envelope proposal, not proven cut endpoints:
    interrupted lines and diagonal cuts are preserved as explicit uncertainties.
    The search is bounded to nearby intervals; a distant collinear member cannot
    silently extend this one. No target length is used to manufacture points.
    """
    if not member or not member.get('axis') or not member.get('polygon'):return None
    a,b=np.asarray(member['axis']['a']),np.asarray(member['axis']['b'])
    length=float(np.linalg.norm(b-a))
    if length<1e-6:return None
    u=(b-a)/length;n=np.array([-u[1],u[0]])
    width=float(np.ptp((np.asarray(member['polygon'])-a)@n))
    if width<=0:return None
    hits=[]
    for stroke in geometry.get('strokes',[]):
        xy=np.asarray([stroke['a'],stroke['b']]);vector=xy[1]-xy[0];span=float(np.linalg.norm(vector))
        if span<2 or abs(np.dot(vector,u))/span<math.cos(math.radians(.4)):continue
        offsets=(xy-a)@n
        if max(abs(abs(offsets)-width/2))>max(.7,width*.06):continue
        coords=(xy-a)@u
        hits.append({'start':float(min(coords)),'end':float(max(coords)),
                     'side':1 if sum(offsets)>0 else -1,'stroke_id':stroke['id'],
                     'primitive_ids':stroke.get('primitive_ids',[]),'a':stroke['a'],'b':stroke['b']})
    # Walk outward from the observed interval. Gaps wider than six section
    # widths are not joined; every smaller gap still remains unverified below.
    lo,hi=0.,length;chosen=[]
    while True:
        found=[h for h in hits if h['end']>=lo-width*6 and h['start']<=hi+width*6]
        if not found:break
        low=min(lo,min(h['start'] for h in found));high=max(hi,max(h['end'] for h in found))
        chosen=found
        if abs(low-lo)+abs(high-hi)<1e-7:break
        lo,hi=low,high
    if not chosen:return None
    sides={str(side):_merge([(h['start'],h['end']) for h in chosen if h['side']==side]) for side in (-1,1)}
    outside={side:sum(max(0,min(0,y)-x)+max(0,y-max(length,x)) for x,y in intervals)*ratio
             for side,intervals in sides.items()}
    extended=lo < -TOLERANCE_MM/ratio or hi>length+TOLERANCE_MM/ratio
    supported=extended and min(outside.values())>max(TOLERANCE_MM,width*ratio)
    xy=np.array([a+u*lo,a+u*hi]);center=np.array([(bbox[0]+bbox[2])/2,(bbox[1]+bbox[3])/2])
    local=np.column_stack(((xy[:,0]-center[0])*ratio,(center[1]-xy[:,1])*ratio,np.zeros(2)))
    return {'status':'native_lines_support_missing_span' if supported else 'no_independent_missing_span_confirmation',
            'observed_axis_pt':member['axis'],
            'axis_pt':{'a':xy[0].tolist(),'b':xy[1].tolist()},'axis_local':local.tolist(),
            'candidate_length_mm':float((hi-lo)*ratio),'observed_length_mm':length*ratio,
            'extension_candidate_mm':float((hi-lo-length)*ratio),
            'native_extension_coverage_mm_by_side':outside,'side_intervals_pt':sides,
            'strokes':chosen,'full_endpoints_verified':False,
            'note':'外包轴端来自原生轮廓延伸；板下中断和端部斜切仍待复核，不是按 BOM 拉伸得到。'}


def infer_missing_face(faces,scene,target_id,sources,input_digest):
    target=next((f for f in faces if f['id']==target_id),None)
    if target is None or target['units']!='mm':raise ValueError('请选择有毫米比例的结构面。')
    pairs=_shared_pairs(faces,scene,target)
    choices=[(a,b) for a,b in itertools.combinations(pairs,2) if a[1]['face_id']!=b[1]['face_id']]
    if not choices:raise ValueError('至少需要两个已连接邻面提供独立公共杆件；当前不能反算缺失面。')
    pair_a,pair_b=max(choices,key=lambda row:min(p[0]['bom_length_mm'] for p in row))
    targets,refs=zip(pair_a,pair_b)
    world=np.array([transformed([e['a'],e['b']],scene['poses'][e['face_id']]) for e in refs])
    if np.sum((world[0]-world[1][::-1])**2)<np.sum((world[0]-world[1])**2):world[1]=world[1][::-1]
    corners=np.array([world[0,0],world[0,1],world[1,1],world[1,0]])
    center=corners.mean(axis=0);_,singular,vt=np.linalg.svd(corners-center)
    if singular[1]<TOLERANCE_MM:raise ValueError('公共边几乎共线，无法确定缺失面的轮廓。')
    normal=vt[-1]
    if normal[2]<0:normal=-normal
    signed=(corners-center)@normal;flat=corners-signed[:,None]*normal
    lengths=np.linalg.norm(np.roll(corners,-1,axis=0)-corners,axis=1)
    prediction={'basis':'existing_neighbor_poses_and_shared_member_axes_only',
                'reference_edge_ids':[e['id'] for e in refs],'marks':[e['mark'] for e in refs],
                'raw_corners_world_mm':corners.tolist(),'planar_corners_world_mm':flat.tolist(),
                'edge_lengths_mm':lengths.tolist(),'widths_mm':[float(lengths[3]),float(lengths[1])],
                'diagonals_mm':[math.dist(corners[0],corners[2]),math.dist(corners[1],corners[3])],
                'planarity_max_mm':float(max(abs(signed))),'planarity_rms_mm':float(np.sqrt(np.mean(signed**2))),
                'plane_normal':normal.tolist(),'tilt_to_ground_deg':math.degrees(math.acos(float(np.clip(abs(normal[2]),0,1)))),
                'boundary_verified':False,'note':'连接两条公共杆端形成候选四边形；两条横向端边为推断，尚非确认的实体边界。'}
    observed=[np.array([e['a'],e['b']]) for e in targets]
    original_fit=_rigid_fit(observed,world)
    # The prediction above uses no coordinates or lengths from the held-out view.
    source=next((s for s in sources if s['id']==target.get('source_id')),None)
    model=next((v for v in source['model']['views'] if v['view_id']==target['view_id']),None) if source else None
    native=next((g for g in source.get('geometry',[]) if g['view_id']==target['view_id']),{}) if source else {}
    observations={m['id']:m for m in source.get('member_observations',[])} if source else {}
    rows=[];proposals=[];issues=[]
    for i,(edge,ref) in enumerate(zip(targets,refs)):
        evidence=native_axis_evidence(observations.get(edge.get('member_id')),native,target['scale']['mm_per_pt'],model['bbox_pt']) if model else None
        row={'mark':edge['mark'],'member_id':edge.get('member_id'),'target_edge_id':edge['id'],
             'reference_edge_id':ref['id'],'bom_length_mm':edge['bom_length_mm'],
             'recognized_length_mm':edge['length'],'neighbor_length_mm':ref['length'],
             'recognized_minus_bom_mm':edge['length']-edge['bom_length_mm'],
             'neighbor_minus_bom_mm':ref['length']-edge['bom_length_mm'],'native_evidence':evidence}
        row['classification']='unresolved'
        if evidence and evidence['status']=='native_lines_support_missing_span':
            row['classification']='recognition_missing_span_supported'
            issues.append(f"件 {edge['mark']}：原图两侧轮廓均延伸到当前识别段以外，支持识别漏段；准确切口仍待核对。")
        if abs(row['neighbor_minus_bom_mm'])>max(TOLERANCE_MM,edge['bom_length_mm']*.01):
            issues.append(f"件 {edge['mark']}：已拼邻面轴长比 BOM 差 {row['neighbor_minus_bom_mm']:.1f} mm，反算基准也须复核。")
        proposals.append(evidence['axis_local'] if evidence and evidence['status']=='native_lines_support_missing_span' else observed[i].tolist())
        rows.append(row)
    native_fit=_rigid_fit(proposals,world) if any(r['classification']=='recognition_missing_span_supported' for r in rows) else None
    native_outline=None
    if native_fit:
        candidate=np.asarray([p[::-1] if flip else p for p,flip in zip(proposals,native_fit['endpoint_flips'])])
        native_outline={'side_lengths_mm':[math.dist(*p) for p in candidate],
                        'widths_mm':[math.dist(candidate[0,j],candidate[1,j]) for j in (0,1)],
                        'corners_local_mm':[candidate[0,0].tolist(),candidate[0,1].tolist(),candidate[1,1].tolist(),candidate[1,0].tolist()],
                        'boundary_verified':False}
    edges={e['id']:e for f in faces for e in f['edges']}
    from .assembly import link_residual
    component=connected_component(scene['front_face_id'],scene['links'],edges)
    pending=[]
    for link in scene['links']:
        if edges[link['a']]['face_id'] not in component:continue
        residual=link_residual(link,edges,scene['poses'])
        if not residual['valid']:pending.append({**link,**residual})
    if pending:issues.append(f'当前装配有 {len(pending)} 条接缝超差，反算轮廓只用于定位检查，不能作为原图错误的证明。')
    if prediction['planarity_max_mm']>TOLERANCE_MM:issues.append('邻面给出的四个端点不共面，应先复核现有面姿态，不能直接补成平面。')
    if native_fit and native_fit['max_gap_mm']>TOLERANCE_MM:issues.append(f"原生外包轴刚性试拼仍有 {native_fit['max_gap_mm']:.1f} mm 间隙；按邻面轮廓修正后也须保留这项原始差异。")
    return {'schema_version':'1.0','target_face_id':target_id,'title':target['title'],
            'artifact_key':digest([input_digest,scene,target_id]),
            'basis_signature':review_signature(input_digest,scene),'prediction':prediction,
            'comparisons':rows,'original_fit':original_fit,'native_candidate_fit':native_fit,
            'native_candidate_outline':native_outline,
            'native_candidate_axes_local':proposals,'upstream_pending_links':len(pending),
            'diagnosis':'recognition_missing_span_supported' if any(r['classification']=='recognition_missing_span_supported' for r in rows) else 'evidence_inconclusive',
            'issues':issues,'source_drawing_error_confirmed':False,'engineering_verified':False,
            'input_geometry_modified':False,'source_id':target.get('source_id'),'view_id':target['view_id'],
            'source_sha256':target.get('source_sha256'),'bbox_pt':model['bbox_pt'] if model else None,
            'source_mm_per_pt':target.get('scale',{}).get('mm_per_pt'),
            'page':target.get('page')}


def write_review_artifacts(report,jobs_root,destination,source_pdf=None):
    """Derived native-PDF crop with observed and proposed axes. Never edits PDF."""
    import base64
    from html import escape
    import fitz
    from .io import file_digest, write_json

    destination.mkdir(parents=True,exist_ok=True)
    source=source_pdf if source_pdf is not None else jobs_root/report['source_id']/'source.pdf'
    if not report.get('bbox_pt') or not report.get('page'):
        report['source_overlay_available']=False
    else:
        if file_digest(source)!=report['source_sha256']:
            raise ValueError('原 PDF 已变化，请重新载入并计算。')
        with fitz.open(source) as doc:
            page=doc[report['page']-1];clip=fitz.Rect(report['bbox_pt'])&page.rect
            pixmap=page.get_pixmap(matrix=fitz.Matrix(1.7,1.7),clip=clip,alpha=False)
        png=pixmap.tobytes('png');(destination/'missing-face-source.png').write_bytes(png)
        # Embed the crop so the exported SVG stays usable outside the server.
        svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{clip.x0} {clip.y0} {clip.width} {clip.height}">',
             '<title>原图回查：黄色为原生轮廓延伸候选，红色为原识别段；切口仍待复核</title>',
             f'<image x="{clip.x0}" y="{clip.y0}" width="{clip.width}" height="{clip.height}" href="data:image/png;base64,{base64.b64encode(png).decode()}"/>']
        for row in report['comparisons']:
            evidence=row['native_evidence']
            if not evidence:continue
            a,b=evidence['axis_pt']['a'],evidence['axis_pt']['b']
            svg.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#ffbf00" stroke-width="7" opacity=".75"/>')
            start,end=evidence['observed_axis_pt']['a'],evidence['observed_axis_pt']['b']
            svg.append(f'<line x1="{start[0]}" y1="{start[1]}" x2="{end[0]}" y2="{end[1]}" stroke="#d53d3d" stroke-width="2" stroke-dasharray="9 5"/>')
            mid=(np.asarray(a)+b)/2
            svg.append(f'<text x="{mid[0]}" y="{mid[1]-12}" fill="#9a4b00" stroke="white" stroke-width="3" paint-order="stroke" font-size="18">件 {escape(str(row["mark"]))} · 原生外包轴 {evidence["candidate_length_mm"]:.1f} mm</text>')
        svg.append('</svg>')
        (destination/'missing-face-source.svg').write_text('\n'.join(svg),encoding='utf-8')
        report['source_overlay_available']=True
    write_json(destination/'missing-face-review.json',report)
