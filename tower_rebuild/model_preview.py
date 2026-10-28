"""Final recognition inventory and rotatable per-view solid previews.

The model remains in each drawing's projection. Unknown assembly transforms,
angle roll and front/rear distances are never promoted to engineering facts.
"""
import copy
import math
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon

from .io import read_json, write_json, file_digest, digest
from .materials import classify
from .topology_features import contour_candidates


def rectangle_sides(points):
    p=Polygon(points)
    if not p.is_valid or p.area<=0:return None
    coords=list(p.minimum_rotated_rectangle.exterior.coords)
    return sorted(math.dist(coords[i],coords[i+1]) for i in (0,1))


def snap_native_junctions(strokes,tolerance=.7):
    """Close sub-point T-junction gaps; no long missing plate edge is invented."""
    result=copy.deepcopy(strokes)
    if not result:return result,[]
    a=np.array([s['a'] for s in strokes]);b=np.array([s['b'] for s in strokes]);d=b-a
    lengths=np.linalg.norm(d,axis=1);changes=[]
    for i,s in enumerate(result):
        if lengths[i]<1e-6:continue
        u=d[i]/lengths[i]
        for end in ('a','b'):
            pt=np.array(s[end]);t=np.sum((pt-a)*d,axis=1)/np.maximum(lengths**2,1e-12)
            nearest=a+t[:,None]*d;distance=np.linalg.norm(nearest-pt,axis=1)
            cross=np.abs(d[:,0]*u[1]-d[:,1]*u[0])/np.maximum(lengths,1e-12)
            valid=(t>.01)&(t<.99)&(distance<tolerance)&(cross>.15)&(np.arange(len(result))!=i)
            if valid.any():
                j=int(np.argmin(np.where(valid,distance,np.inf)))
                s[end]=nearest[j].tolist()
                if distance[j]>1e-6:changes.append({'primitive_ids':s['primitive_ids'],'onto_primitive_ids':strokes[j]['primitive_ids'],
                    'from_pt':pt.tolist(),'to_pt':s[end],'distance_pt':float(distance[j])})
    return result,changes


def view_scale(view,polygons,members,plates,holes):
    """Prefer dimension spans, then explicit hole dimensions; report fallback."""
    evidence=[];basis=None
    for p in polygons:
        if p['view_id']!=view['id'] or p.get('closure',{}).get('status')!='consistent_with_rounding':continue
        for e in p['edges']:
            distance=math.dist(e['structural_a_pt'],e['structural_b_pt'])
            if distance>1:evidence.append({'ratio':e['value_mm']/distance,'id':e['text_id']})
    if evidence:basis='dimension_span_projection_candidate'
    if not evidence:
        evidence=[{'ratio':h['diameter_mm']/(2*min(h['radii_pt'])),'id':h['id']} for h in holes if h.get('diameter_mm') and min(h['radii_pt'])>0]
        if evidence:basis='explicit_diameter_projection_candidate'
    if not evidence:
        evidence=[{'ratio':m['overlap_recovery']['scale_evidence']['mm_per_pt_candidate'],'id':m['id']} for m in members
                  if (m.get('overlap_recovery',{}).get('scale_evidence') or {}).get('mm_per_pt_candidate')]
        if evidence:basis='bom_member_consensus_preview_only'
    if not evidence and view['role']=='detail':
        for p in plates:
            stock=classify(p['bom']['specification']);sides=rectangle_sides(p['polygon']) if p.get('polygon') else None
            if sides and stock.get('width_mm'):
                target=sorted([stock['width_mm'],p['bom']['length_mm']]);ratios=[t/s for t,s in zip(target,sides)]
                if max(ratios)/min(ratios)<1.08:evidence.append({'ratio':float(np.mean(ratios)),'id':p['id']})
        if evidence:basis='bom_detail_dimensions_preview_only'
    if not evidence and view['role']=='section_view':
        # A section can contain transverse cuts of long chords plus one full
        # longitudinal member. Reject enormous cut-length/projected-cut ratios.
        page_ratios=[e['value_mm']/math.dist(e['structural_a_pt'],e['structural_b_pt']) for p in polygons
            if p.get('closure',{}).get('status')=='consistent_with_rounding'
            and p.get('page',view.get('page'))==view.get('page') for e in p['edges']
            if math.dist(e['structural_a_pt'],e['structural_b_pt'])>1]
        for m in members:
            if not m.get('axis'):continue
            span=math.dist(m['axis']['a'],m['axis']['b'])
            if span<=1:continue
            r=m['bom']['length_mm']/span
            if any(abs(r/reference-1)<.06 for reference in page_ratios):evidence.append({'ratio':r,'id':m['id']})
        if evidence:basis='single_bom_span_with_page_scale_crosscheck_preview_only'
    if not evidence:return {'mm_per_pt':None,'basis':'unresolved','evidence':[],'verified':False}
    median=float(np.median([e['ratio'] for e in evidence]));kept=[e for e in evidence if abs(e['ratio']/median-1)<.12]
    if len(kept)<=len(evidence)/2:
        return {'mm_per_pt':None,'basis':'conflicting_projection_scales','evidence':evidence,'verified':False}
    return {'mm_per_pt':float(np.median([e['ratio'] for e in kept])),'basis':basis,'evidence':kept,
            'max_relative_spread':max(abs(e['ratio']/median-1) for e in kept),'verified':False}


def plate_outline(part,contours,scale):
    stock=classify(part['bom']['specification']);options=[]
    if not scale or not stock.get('width_mm') or not stock.get('thickness_mm'):return None,[]
    target=sorted([stock['width_mm'],part['bom']['length_mm']]);box=part['callout_bbox_pt']
    label=Point((box[0]+box[2])/2,(box[1]+box[3])/2)
    for c in contours:
        p=Polygon(c['polygon']);sides=rectangle_sides(c['polygon'])
        if not sides or not c['hole_ids']:continue
        distance=p.distance(label)
        if distance>85:continue
        xy=np.array(c['polygon']);envelopes=[(sides,None)]
        for a,b in zip(xy,xy[1:]):
            d=b-a;length=np.linalg.norm(d)
            if length<2:continue
            u=d/length;n=np.array([-u[1],u[0]])
            envelopes.append((sorted([float(np.ptp(xy@u)),float(np.ptp(xy@n))]),u.tolist()))
        fits=[([abs(side*scale/length-1) for side,length in zip(dims,target)],direction) for dims,direction in envelopes]
        errors,envelope_direction=min(fits,key=lambda f:sum(f[0]))
        if max(errors)>.18:continue
        # A native cell is not a plate merely because it encloses a hole.
        # The stock envelope is a rejection check, not a generated rectangle.
        candidate={'contour_id':c['id'],'polygon_pt':c['polygon'],'primitive_ids':c['primitive_ids'],
                   'hole_ids':c['hole_ids'],'stock_envelope_relative_errors':errors,'distance_to_callout_pt':distance,
                   'stock_envelope_direction':envelope_direction,
                   'basis':c.get('preview_basis','existing_native_closed_region'),'junction_snap_tolerance_pt':c.get('junction_snap_tolerance_pt'),
                   'score':sum(errors)*100+distance*.15,'engineering_verified':False}
        duplicate=next((o for o in options if Polygon(o['polygon_pt']).intersection(p).area/max(Polygon(o['polygon_pt']).union(p).area,1e-6)>.9),None)
        if duplicate:
            if candidate['score']<duplicate['score']:options.remove(duplicate)
            else:continue
        options.append(candidate)
    options.sort(key=lambda o:o['score'])
    if not options or (len(options)>1 and options[1]['score']-options[0]['score']<6):return None,options[:5]
    return options[0],options[:5]


def explicit_fan_holes(view,holes,strokes):
    """A single count/diameter label with native leaders to every hole centre."""
    result=copy.deepcopy(holes);annotations=view.get('diameter_annotations',[])
    if len(annotations)!=1 or len(holes)<2 or annotations[0]['count']!=len(holes):return result
    annotation=annotations[0];box=annotation['bbox_pt'];anchor=Point((box[0]+box[2])/2,(box[1]+box[3])/2);tips=[];evidence=[]
    for h in holes:
        options=[]
        for s in strokes:
            for a,b in ((s['a'],s['b']),(s['b'],s['a'])):
                if math.dist(b,h['center_pt'])<.65 and anchor.distance(Point(a))<20:options.append((a,s))
        if len(options)!=1:return result
        tips.append(options[0][0]);evidence.extend(options[0][1]['primitive_ids'])
    if max(math.dist(t,tips[0]) for t in tips)>.65:return result
    for h in result:
        h.update(diameter_mm=annotation['diameter_mm'],diameter_evidence=[annotation['primitive_id']]+sorted(set(evidence)),
                 diameter_basis='explicit_count_and_shared_native_leaders_to_all_centres')
    return result


def build_preview(run_dir):
    run_dir=Path(run_dir);site=run_dir/'site';top=site/'topology'
    manifest=read_json(run_dir/'manifest.json');report=read_json(top/'report.json')
    from .ocr_input import analysis_source
    analysis_source(manifest)
    if file_digest(manifest['source'])!=manifest['source_sha256'] or report['source_sha256']!=manifest['source_sha256']:
        raise ValueError('Source PDF changed; regenerate topology and audit')
    if any(file_digest(site/name)!=value for name,value in report['input_files'].items()):
        raise ValueError('Topology inputs changed; regenerate topology')
    audit=read_json(site/'audit/report.json')
    audit_files=[top/'report.json',top/'views.json',top/'members.json',top/'geometry.json',site/'tables/bom.json',site/'structure/primitives.json',site/'structure/callouts.json']
    if audit['input_sha256']!=digest({str(p.relative_to(site)):file_digest(p) for p in audit_files}):
        raise ValueError('Dimension audit is stale; run audit again')
    data={name:read_json(top/f'{name}.json') for name in ('views','members','plates','holes','geometry')}
    bom=read_json(site/'tables/bom.json');primitives=read_json(site/'structure/primitives.json');callouts=read_json(site/'structure/callouts.json')
    models=[];summaries=[];all_issues=[]
    for view in data['views']:
        vid=view['id'];select=lambda name:[x for x in data[name] if x['view_id']==vid]
        members,plates,holes=map(select,('members','plates','holes'));geometry=select('geometry')[0]
        holes=explicit_fan_holes(view,holes,geometry['strokes'])
        scale=view_scale(view,audit['polygons'],members,plates,holes);ratio=scale['mm_per_pt']
        snapped,changes=snap_native_junctions(geometry['strokes'])
        extra=contour_candidates(view,snapped,holes)
        for i,c in enumerate(extra):c.update(id=f'{vid}_preview_contour_{i+1:03d}',preview_basis='native_outline_with_subpoint_junction_snap',junction_snap_tolerance_pt=.7)
        contours=geometry['contours']+extra
        output={'view_id':vid,'title':view['title'],'source_sha256':manifest['source_sha256'],'bbox_pt':view['bbox_pt'],'scale':scale,'members':[],'plates':[],'holes':[],
                'issues':[],'native_junction_snaps':changes,'assembly_transform':None}
        def issue(kind,object_id,mark,message):
            item={'kind':kind,'view_id':vid,'object_id':object_id,'mark':mark,'message':message};output['issues'].append(item);all_issues.append(item)
        for m in members:
            section=classify(m['bom']['specification']);axis=m.get('axis')
            if section['kind']!='angle' or not axis or ratio is None:
                issue('member_not_modeled',m['id'],m['mark'],'杆件缺少可用轴线、截面或本面比例');continue
            length=math.dist(axis['a'],axis['b'])*ratio;delta=length-m['bom']['length_mm']
            if view['role']=='section_view' and length<m['bom']['length_mm']*.12:
                issue('member_transverse_cut',m['id'],m['mark'],'本处疑似长杆横截面，不把截面短线当成纵向杆件；空间方向待定');continue
            output['members'].append({'id':m['id'],'mark':m['mark'],'side':m.get('side'),'axis_pt':axis,'section':section,
                'specification':m['bom']['specification'],'bom_length_mm':m['bom']['length_mm'],'projected_preview_length_mm':length,
                'delta_to_bom_mm':delta,'hole_ids':m['hole_ids'],'overlap_status':m['overlap_recovery']['status'],
                'axis_source':'current_topology_including_supported_overlap_candidates','endpoints_verified':False,'angle_roll_verified':False})
            if abs(delta)>max(5,m['bom']['length_mm']*.03):issue('member_length_or_projection',m['id'],m['mark'],f'模型投影长 {length:.1f} / BOM {m["bom"]["length_mm"]:g} mm；缺长、投影或圈号需核对')
            if m.get('ambiguous'):issue('member_association',m['id'],m['mark'],'杆件轮廓对应有多个候选')
            if m['overlap_recovery']['status'] in {'partial_overlap_recovered_candidate','extent_unresolved','ambiguous_end_allocation'}:
                issue('member_incomplete',m['id'],m['mark'],'完整杆端仍未确定，三维保留现有识别范围')
        for p in plates:
            chosen,options=plate_outline(p,contours,ratio);stock=classify(p['bom']['specification'])
            record={'id':p['id'],'mark':p['mark'],'side':p.get('side'),'specification':p['bom']['specification'],'stock':stock,
                    'bom_length_mm':p['bom']['length_mm'],'callout_bbox_pt':p['callout_bbox_pt'],'outline':chosen,
                    'outline_candidates':options,'drill_holes':[],'original_outline_present':bool(p.get('polygon'))}
            if chosen:
                shape=Polygon(chosen['polygon_pt'])
                for h in holes:
                    if h['id'] in chosen['hole_ids'] and h.get('diameter_mm') and shape.covers(Point(h['center_pt']).buffer(h['diameter_mm']/ratio/2)):
                        record['drill_holes'].append({'id':h['id'],'center_pt':h['center_pt'],'diameter_mm':h['diameter_mm'],'evidence':h['diameter_evidence']})
            else:issue('plate_outline_unresolved',p['id'],p['mark'],'板件有多个可行轮廓' if options else '未找到与板件尺寸相符的闭合多边形；不生成矩形替代')
            if p.get('polygon') and not any(o['contour_id']==p.get('contour_id') for o in options):
                issue('previous_plate_outline_rejected',p['id'],p['mark'],'原轮廓候选与 BOM 板坯尺寸不符，三维不采用该轮廓')
            output['plates'].append(record)
        for h in holes:
            output['holes'].append({'id':h['id'],'center_pt':h['center_pt'],'radii_pt':h['radii_pt'],
                'diameter_mm':h['diameter_mm'],'member_ids':h['member_ids'],
                'diameter_evidence':h.get('diameter_evidence',[]),'diameter_basis':h.get('diameter_basis','existing_explicit_annotation' if h['diameter_mm'] else None),
                'plate_ids':[p['id'] for p in output['plates'] if p['outline'] and h['id'] in p['outline']['hole_ids']],
                'kind':'explicit_diameter_projection' if h['diameter_mm'] else 'unconfirmed_hole_or_bolt_projection'})
        counts={'member_observations':len(members),'angle_models':len(output['members']),
                'member_incomplete':sum(i['kind']=='member_incomplete' for i in output['issues']),
                'member_length_or_projection_flags':sum(i['kind']=='member_length_or_projection' for i in output['issues']),
                'plate_observations':len(plates),'plate_polygons':sum(bool(p['outline']) for p in output['plates']),
                'plate_shapes_pending':sum(not p['outline'] for p in output['plates']),
                'hole_projections':len(holes),'explicit_hole_diameters':sum(h['diameter_mm'] is not None for h in holes),
                'hole_owner_pending':sum(not h['member_ids'] and not h['plate_ids'] for h in output['holes'])}
        output['counts']=counts;models.append(output);summaries.append({'view_id':vid,'title':view['title'],'counts':counts,'status':'needs_review'})
    observed={m['mark'] for m in data['members']};plate_marks={p['mark'] for p in data['plates']}
    missing_members=[b['mark'] for b in bom if classify(b['specification'])['kind']=='angle' and b['mark'] not in observed]
    missing_plates=[b['mark'] for b in bom if classify(b['specification'])['kind']=='plate' and b['mark'] not in plate_marks]
    covered={pid for h in data['holes'] for pid in h['primitive_ids']}|{pid for c in callouts for pid in c['circle_ids']}
    unassigned_circles=[p['id'] for p in primitives if p['part']=='structure' and p['primitive_type']=='circle' and .25<=p['geometry']['radius']<=8 and p['id'] not in covered]
    unresolved_arcs=[p['id'] for p in primitives if p['part']=='structure' and p['primitive_type']=='circular_arc' and .25<=p['geometry']['radius']<=8 and p['id'] not in covered]
    count=Counter()
    for s in summaries:count.update(s['counts'])
    result={'schema_version':'1.0','stage':'per_view_basic_3d_preview','status':'needs_review','source_sha256':manifest['source_sha256'],
            'input_files':{str(p.relative_to(site)):file_digest(p) for p in audit_files+[top/'plates.json',top/'holes.json',site/'audit/report.json']},
            'coordinate_system':'PDF-point projection with BOM section sizes divided by candidate mm_per_pt; each face independent',
            'views':models,'engineering_accuracy_verified':False,
            'display_assumptions':['每个结构图独立建模，前后上下空间拼装尚未确认。','角钢肢宽、厚度及板厚按 BOM；杆件位置和轴长保留图纸投影及已有补段。',
                '角钢朝向、前后分层间距是预览假设；旋转用于检查，不表示已确定实际空间关系。',
                '有明确孔径且位于板内的孔才切穿实体，其余圆形仅作为孔/螺栓投影标记。']}
    inventory={'schema_version':'1.0','source_sha256':manifest['source_sha256'],'status':'needs_review','counts':dict(count),
               'bom_angle_marks':sum(classify(b['specification'])['kind']=='angle' for b in bom),'observed_angle_marks':len(observed),
               'missing_member_marks':missing_members,'missing_plate_marks':missing_plates,'unassigned_small_circle_ids':unassigned_circles,
               'unresolved_small_arc_ids':unresolved_arcs,'views':summaries,'issues':all_issues,'dimension_findings':audit['findings'],
               'completeness_verified':False,'notes':['观察数不是实物数量，重复件号不能作为完整数量验收。',
                 '未归属小圆和短圆弧仍需核对，不能据已识别孔数宣称孔位识别完整。']+result['display_assumptions']}
    write_json(top/'model-preview.json',result);write_json(top/'final-check.json',inventory)
    # Install local modules/assets; never require a CDN or a network model call.
    shutil.copytree(Path(__file__).parent/'topology_web',top,dirs_exist_ok=True)
    index=top/'index.html';html=index.read_text(encoding='utf-8')
    if 'id="audit-link"' not in html:html=html.replace('href="graph.json" download','id="audit-link" href="../audit/">尺寸初审</a> · <a href="graph.json" download',1)
    index.write_text(html,encoding='utf-8')
    lines=['# 建模前识别检查','',f'BOM 角钢件号 {inventory["bom_angle_marks"]}，已观察到 {len(observed)}；缺失件号：{missing_members}。',
           f'连接板缺失件号：{missing_plates}；未归属小圆 {len(unassigned_circles)} 个，未完整归组小圆弧 {len(unresolved_arcs)} 条。','',
           '| 结构面 | 角钢模型 / 杆件观察 | 杆端未完整 | 板形 / 板件观察 | 孔位投影 / 明确孔径 |','| --- | --- | ---: | --- | --- |']
    for s in summaries:
        c=s['counts'];lines.append(f'| {s["title"]} | {c["angle_models"]} / {c["member_observations"]} | {c["member_incomplete"]} | {c["plate_polygons"]} / {c["plate_observations"]} | {c["hole_projections"]} / {c["explicit_hole_diameters"]} |')
    lines.extend(['','## 模型与未确定条件','',*['- '+s for s in inventory['notes']],'','## 逐项疑点',''])
    names={v['id']:v['title'] for v in data['views']}
    for i in all_issues:lines.append(f'- {names[i["view_id"]]} / 件号 {i["mark"]} / {i["object_id"]}：{i["message"]}。')
    (run_dir/'model-preflight.md').write_text('\n'.join(lines),encoding='utf-8')
    failure=run_dir/'failure.json'
    if failure.exists() and read_json(failure).get('stage')=='preview':failure.unlink()
    print(f'[preview] {dict(count)}; missing angle marks={missing_members}; status=needs_review',flush=True)
    return inventory
