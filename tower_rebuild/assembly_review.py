"""Explain assembly blockers using source member geometry, without refitting it."""
from collections import defaultdict
import itertools
import math

import numpy as np

from .assembly import TOLERANCE_MM


def direction_angle(a, b):
    u=np.asarray(a['b'])-a['a'];v=np.asarray(b['b'])-b['a']
    cosine=abs(float(np.dot(u,v)/(np.linalg.norm(u)*np.linalg.norm(v))))
    return math.degrees(math.acos(float(np.clip(cosine,0,1))))


def review_assembly(faces, matches, parallel, scene):
    lookup={f['id']:f for f in faces};edges={e['id']:e for f in faces for e in f['edges']}
    reports=[];length_conflicts=[];overlaps=[];mark_conflicts=[]
    for face in faces:
        reasons=[]
        if face['units']!='mm':reasons.append('scale_missing')
        if not face['members'] and not any(p.get('polygon') for p in face.get('plates',[])):reasons.append('no_solid_geometry')
        if len(face['polygon'])<3:reasons.append('boundary_missing')
        if face.get('scale',{}).get('basis')=='bom_member_consensus_preview_only':reasons.append('scale_from_bom_preview')
        observed=[e for e in face['edges'] if e.get('member_id') or e.get('basis')=='observed_member_axis']
        if face['units']=='mm':
            for edge in observed:
                cut=edge.get('bom_length_mm')
                if cut is None:continue
                tolerance=max(TOLERANCE_MM,cut*.01)
                if abs(edge['length']-cut)>tolerance:
                    length_conflicts.append({'face_id':face['id'],'edge_id':edge['id'],'mark':edge['mark'],
                        'observed_length_mm':edge['length'],'bom_length_mm':cut,
                        'difference_mm':edge['length']-cut,'review_threshold_mm':tolerance})
            for a,b in itertools.combinations(observed,2):
                if a['mark']==b['mark']:continue
                residual=min(max(math.dist(a['a'],b['a']),math.dist(a['b'],b['b'])),
                             max(math.dist(a['a'],b['b']),math.dist(a['b'],b['a'])))
                if residual<=TOLERANCE_MM:
                    overlaps.append({'face_id':face['id'],'a':a['id'],'b':b['id'],'marks':[a['mark'],b['mark']],
                                     'max_axis_gap_mm':residual,'status':'check_projection_or_axis_assignment'})
        reports.append({'face_id':face['id'],'reasons':reasons,'member_count':len(face['members']),
                        'boundary_member_count':sum(e['boundary'] for e in observed)})

    by_mark=defaultdict(list)
    for edge in edges.values():
        if edge.get('mark'):by_mark[edge['mark']].append(edge)
    for mark,items in by_mark.items():
        for a,b in itertools.combinations(items,2):
            if a['face_id']==b['face_id']:continue
            reasons=[]
            if a['specification']!=b['specification']:reasons.append('specification_conflict')
            if a['bom_length_mm'] is not None and b['bom_length_mm'] is not None and abs(a['bom_length_mm']-b['bom_length_mm'])>TOLERANCE_MM:reasons.append('bom_length_conflict')
            if lookup[a['face_id']]['units']=='mm' and lookup[b['face_id']]['units']=='mm' and abs(a['length']-b['length'])>max(TOLERANCE_MM,min(a['length'],b['length'])*.01):reasons.append('observed_length_conflict')
            if reasons:mark_conflicts.append({'mark':mark,'a':a['id'],'b':b['id'],'reasons':reasons})

    strong=[m for m in matches if m.get('shared_member_candidate',m['automatic_candidate'])]
    adjacency=defaultdict(set);pairs=defaultdict(list)
    for match in strong:
        a,b=match['face_a'],match['face_b'];adjacency[a].add(b);adjacency[b].add(a)
        pairs[(a,b)].append((match['a'],match['b']))
        pairs[(b,a)].append((match['b'],match['a']))
    components=[];seen=set()
    for fid in lookup:
        if fid in seen:continue
        found=set();todo=[fid]
        while todo:
            node=todo.pop()
            if node in found:continue
            found.add(node);todo.extend(adjacency[node]-found)
        seen|=found;components.append(sorted(found))

    bridges=[]
    tapers=scene.get('taper_rules',[])+([scene['taper_rule']] if scene.get('taper_rule') else [])
    for relation in parallel:
        a,b=relation['a'],relation['b']
        tapered=any({a,b}=={taper['a'],taper['b']} for taper in tapers)
        for via in sorted(adjacency[a]&adjacency[b]):
            for (_,ea),(_,eb) in itertools.product(pairs[(a,via)],pairs[(b,via)]):
                if ea==eb:continue
                angle=direction_angle(edges[ea],edges[eb])
                bridges.append({'a':a,'b':b,'via':via,'edge_a':ea,'edge_b':eb,
                                'marks':[edges[ea]['mark'],edges[eb]['mark']],
                                'axis_angle_deg':angle,'required_angle_deg':0.,'tolerance_deg':1.,
                                'constraint_kind':'taper' if tapered else 'parallel',
                                'compatible_with_parallel_faces':angle<=1.,
                                'basis':'intersections_of_parallel_planes_with_one_transverse_plane_must_be_parallel'})
    root=scene.get('front_face_id')
    component=next((c for c in components if root in c),[])
    unresolved=[r['face_id'] for r in reports if any(s in r['reasons'] for s in ('scale_missing','no_solid_geometry','boundary_missing'))]
    blockers=[]
    if unresolved:blockers.append({'code':'source_views_incomplete','face_ids':unresolved,
        'message':f'{len(unresolved)} 个现有视图缺少比例、实体或面边界，先核对本图识别结果；必要时再补充图纸。'})
    if len(component)<4:blockers.append({'code':'insufficient_connected_faces','face_ids':component,
        'message':f'正视参考面通过公共杆件只连到 {len(component)} 个面，尚不足以构成四面环。'})
    if any(b['constraint_kind']=='parallel' and not b['compatible_with_parallel_faces'] for b in bridges):blockers.append({'code':'parallel_bridge_conflict',
        'message':'横向剖面内的公共杆件不平行，与前后面平行约束冲突；需核对收分斜面、剖面表达或轴线识别。'})
    if length_conflicts:blockers.append({'code':'member_length_conflict','message':f'{len(length_conflicts)} 条杆件观察的长度与 BOM 差异超过复核阈值，应先检查杆端与投影。'})
    if mark_conflicts:blockers.append({'code':'same_mark_conflict','message':f'{len(mark_conflicts)} 对同件号观察存在规格、BOM 长度或轴长冲突，须核对其是否为同一实物。'})
    return {'shared_member_count':len(strong),'boundary_match_count':sum(m['automatic_candidate'] for m in matches),
            'components':components,'front_component':component,'face_reports':reports,'blockers':blockers,
            'parallel_bridges':bridges,'length_conflicts':length_conflicts,'overlapping_axes':overlaps,
            'same_mark_conflicts':mark_conflicts,'engineering_verified':False}
