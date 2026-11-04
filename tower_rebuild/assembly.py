"""Rigid assembly of drawing faces, with source-scoped edges and closure checks."""
from collections import Counter, defaultdict
import copy
import itertools
import math

import numpy as np
from scipy.spatial.transform import Rotation
from shapely.geometry import MultiPoint, Point, Polygon, LineString

from .io import digest


FRONT_QUATERNION = Rotation.from_euler('x', 90, degrees=True).as_quat().tolist()
TOLERANCE_MM = 3.0


def pose_matrix(pose):
    position = np.asarray(pose['position'], dtype=float)
    quaternion = np.asarray(pose['quaternion'], dtype=float)
    if position.shape != (3,) or quaternion.shape != (4,) or not np.isfinite(position).all() or not np.isfinite(quaternion).all():
        raise ValueError('面的位置和旋转必须是有限数值。')
    if abs(np.linalg.norm(quaternion)-1) > .001 or np.max(np.abs(position)) > 1e8:
        raise ValueError('面旋转必须为单位四元数，位置不得超出允许范围。')
    matrix = np.eye(4)
    matrix[:3,:3] = Rotation.from_quat(quaternion).as_matrix()
    matrix[:3,3] = position
    return matrix


def matrix_pose(matrix):
    return {'position': np.asarray(matrix[:3,3]).tolist(),
            'quaternion': Rotation.from_matrix(matrix[:3,:3]).as_quat().tolist()}


def transformed(points, pose):
    matrix = pose_matrix(pose)
    return (np.asarray(points) @ matrix[:3,:3].T + matrix[:3,3]).tolist()


def side_name(value):
    if value and ('前' in value or value == 'front'):
        return 'front'
    if value and ('后' in value or value == 'rear'):
        return 'rear'
    return 'unspecified'


def extract_faces(sources, scale_overrides=None):
    """Retain all views. Explicit front/rear groups become separate parallel faces."""
    faces, parallel = [], []
    scale_overrides = scale_overrides or {}
    for source in sources:
        views = {v['id']: v for v in source['views']}
        geometry = {g['view_id']: g for g in source.get('geometry', [])}
        bom = defaultdict(list)
        for row in source.get('bom', []):
            bom[str(row['mark'])].append(row)
        for model in source['model']['views']:
            view = views[model['view_id']]
            members, plates = model['members'], model['plates']
            sides = {side_name(m.get('side')) for m in members}
            split = {'front', 'rear'} <= sides
            groups = [s for s in ('front', 'rear', 'unspecified') if s in sides] if split else ['whole']
            siblings = {}
            for group in groups:
                fid = f"{source['id']}:{view['id']}:{group}"
                siblings[group] = fid
                chosen = [m for m in members if group == 'whole' or side_name(m.get('side')) == group]
                chosen_ids = {m['id'] for m in chosen}
                selected_plates = [p for p in plates if group == 'whole' or side_name(p.get('side')) == group]
                scale = scale_overrides.get(fid, model['scale']['mm_per_pt'])
                if scale is not None and (not isinstance(scale, (int, float)) or not math.isfinite(scale) or not .0001 <= scale <= 1e5):
                    raise ValueError('面比例必须是 0.0001 至 100000 之间的 mm/pt 数值。')
                ratio = scale or 1.0
                box = model['bbox_pt']; origin = [(box[0]+box[2])/2, (box[1]+box[3])/2]
                def xyz(p):
                    return [(p[0]-origin[0])*ratio, (origin[1]-p[1])*ratio, 0.0]
                rods = [{**m, 'a': xyz(m['axis_pt']['a']), 'b': xyz(m['axis_pt']['b'])} for m in chosen]
                for rod in rods:
                    rows = bom.get(str(rod['mark']), [])
                    rod['bom_quantity'] = rows[0].get('quantity') if len(rows) == 1 else None
                panels = [{**p, 'polygon': [xyz(v) for v in p['outline']['polygon_pt']] if p.get('outline') else None}
                          for p in selected_plates]
                for panel in panels:
                    panel['drill_holes_local']=[{**h,'center':xyz(h['center_pt'])} for h in panel.get('drill_holes',[])]
                holes = [{**h, 'center': xyz(h['center_pt']), 'radius': min(h['radii_pt'])*ratio}
                         for h in model['holes'] if group == 'whole' or chosen_ids.intersection(h['member_ids'])]
                points = [p[:2] for m in rods for p in (m['a'], m['b'])]
                points += [p[:2] for panel in panels for p in (panel['polygon'] or [])]
                hull = MultiPoint(points).convex_hull if points else None
                polygon = [[float(x), float(y), 0.] for x,y in list(hull.exterior.coords)[:-1]] if isinstance(hull, Polygon) and hull.area > 1e-6 else []
                edges = []
                for member in rods:
                    a,b = np.array(member['a']),np.array(member['b']); length = float(np.linalg.norm(b-a))
                    if length < 1e-6:
                        continue
                    boundary = False
                    if polygon:
                        for pa,pb in zip(polygon, polygon[1:]+polygon[:1]):
                            line = LineString([pa[:2],pb[:2]])
                            tolerance = max(1., min(15., length*.004))
                            if max(line.distance(Point(a[:2])), line.distance(Point(b[:2]))) <= tolerance:
                                boundary = True
                                break
                    edges.append({'id': fid+':edge:'+member['id'], 'face_id': fid, 'member_id': member['id'],
                                  'mark': member['mark'], 'specification': member['specification'],
                                  'bom_length_mm': member['bom_length_mm'], 'a':a.tolist(), 'b':b.tolist(),
                                  'bom_quantity': member['bom_quantity'],
                                  'length':length, 'boundary':boundary, 'basis':'observed_member_axis',
                                  'endpoints_verified':member.get('endpoints_verified', False)})
                # Envelope edges remain selectable when no individual member
                # spans an entire boundary; they are explicitly unverified.
                for index,(a,b) in enumerate(zip(polygon,polygon[1:]+polygon[:1])):
                    if math.dist(a,b) < 1e-6:
                        continue
                    if any(max(math.dist(a,e['a']),math.dist(b,e['b'])) < 1 or
                           max(math.dist(a,e['b']),math.dist(b,e['a'])) < 1 for e in edges):
                        continue
                    edges.append({'id':fid+f':outline:{index}', 'face_id':fid, 'member_id':None, 'mark':None,
                                  'specification':None, 'bom_length_mm':None, 'a':a, 'b':b, 'length':math.dist(a,b),
                                  'boundary':True, 'basis':'inferred_convex_envelope', 'endpoints_verified':False})
                # A view with no solids is still shown using its original lines.
                wire = [[xyz(s['a']),xyz(s['b'])] for s in geometry.get(view['id'],{}).get('strokes',[])] if not rods and not any(p['polygon'] for p in panels) else []
                label = {'front':' · 前件面','rear':' · 后件面','unspecified':' · 未注明前后件','whole':''}[group]
                faces.append({'id':fid, 'source_id':source['id'], 'source_name':source['name'],
                              'source_sha256':source['model']['source_sha256'], 'view_id':view['id'], 'page':view['page'],
                              'title':view['title']+label, 'side':group, 'role':view.get('face_role','unassigned'),
                              'image_url':source.get('result_url','')+'/topology/'+view.get('image',''),
                              'scale':{'mm_per_pt':scale, 'basis':'manual' if fid in scale_overrides else model['scale']['basis']},
                              'units':'mm' if scale else 'pdf_pt', 'members':rods, 'plates':panels, 'holes':holes,
                              'wire':wire, 'polygon':polygon, 'edges':edges, 'boundary_basis':'inferred_convex_envelope',
                              'source_issues':model.get('issues',[]), 'engineering_verified':False})
            if split:
                parallel.append({'a':siblings['front'], 'b':siblings['rear'],
                                 'basis':'explicit_front_rear_marks', 'offset_mm':None})
    measured=[max(np.ptp(f['polygon'],axis=0)) for f in faces if f['units']=='mm' and f['polygon']]
    display_extent=max(1000.,float(np.median(measured))*.4) if measured else 1000.
    for face in faces:
        points=[e[k] for e in face['edges'] for k in ('a','b')] or [p for line in face['wire'] for p in line]
        extent=float(max(np.ptp(points,axis=0))) if points else 1.
        face['display_scale']=1. if face['units']=='mm' else display_extent/max(extent,1.)
    return faces, parallel


def initial_scene(faces):
    spans = []
    for face in faces:
        pts = [e[k] for e in face['edges'] for k in ('a','b')] or [p for line in face['wire'] for p in line] or [[0,0,0]]
        pts=np.asarray(pts)*face.get('display_scale',1.)
        spans.append((face, np.ptp(pts,axis=0), np.min(pts,axis=0)))
    width = max([s[1][0] for s in spans]+[500.])*1.2
    height = max([s[1][1] for s in spans]+[500.])*1.2
    columns = max(1, math.ceil(math.sqrt(len(faces))))
    poses = {f['id']:{'position':[(i%columns)*width,(i//columns)*height,float(-low[1])],
                       'quaternion':FRONT_QUATERNION[:]} for i,(f,span,low) in enumerate(spans)}
    eligible = [f for f in faces if f['polygon'] and f['units']=='mm' and f['role'] not in {'detail','schematic'}]
    root = max(eligible, key=lambda f:(f['side']=='front' or f['role']=='front',len(f['members'])),default=None)
    return {'poses':poses, 'active_face_ids':[f['id'] for f in eligible], 'front_face_id':root['id'] if root else None,
            'links':[], 'scale_overrides':{}}


def edge_matches(faces, parallel):
    pairs = {frozenset((p['a'],p['b'])) for p in parallel}
    eligible = [e for f in faces if f['units']=='mm' and f['role'] not in {'detail','schematic'} for e in f['edges']]
    result = []
    for a,b in itertools.combinations(eligible,2):
        if a['face_id']==b['face_id'] or frozenset((a['face_id'],b['face_id'])) in pairs:
            continue
        same = bool(a['mark']) and a['mark']==b['mark']
        if not same and not (a['boundary'] and b['boundary']):
            continue
        difference = abs(a['length']-b['length'])
        if difference > max(TOLERANCE_MM, min(a['length'],b['length'])*.01):
            continue
        spec = bool(a['specification']) and a['specification']==b['specification']
        cut = a['bom_length_mm'] is not None and b['bom_length_mm'] is not None and abs(a['bom_length_mm']-b['bom_length_mm'])<=TOLERANCE_MM
        kind = 'mark_spec_length' if same and spec and cut else 'length_only'
        consistent = all(e['bom_length_mm'] is not None and abs(e['length']-e['bom_length_mm'])<=max(TOLERANCE_MM,e['bom_length_mm']*.01) for e in (a,b))
        shared = kind=='mark_spec_length' and consistent
        result.append({'id':digest([a['id'],b['id']])[:16], 'a':a['id'], 'b':b['id'],
                       'face_a':a['face_id'], 'face_b':b['face_id'], 'kind':kind,
                       'length_difference_mm':difference, 'default_angle_deg':90,
                       'member_length_consistent':consistent,'shared_member_candidate':shared,
                       'both_on_envelope':a['boundary'] and b['boundary'],
                       'automatic_candidate':shared and a['boundary'] and b['boundary'], 'engineering_verified':False})
    return sorted(result,key=lambda r:(not r['shared_member_candidate'],not r['automatic_candidate'],r['length_difference_mm'],r['id']))


def attached_pose(parent_edge, child_edge, parent_pose, angle_deg=90, flip=True):
    if not math.isfinite(angle_deg) or not -180 <= angle_deg <= 180:
        raise ValueError('连接角度须在 -180° 至 180° 之间。')
    a,b = np.asarray(transformed([parent_edge['a'],parent_edge['b']],parent_pose))
    c,d = np.asarray([child_edge['a'],child_edge['b']])
    target = (a-b) if flip else (b-a)
    target /= np.linalg.norm(target)
    local = (d-c)/np.linalg.norm(d-c)
    normal = pose_matrix(parent_pose)[:3,:3] @ [0,0,1]
    axis = (b-a)/np.linalg.norm(b-a)
    desired_normal = Rotation.from_rotvec(axis*math.radians(angle_deg)).apply(normal)
    source_basis = np.column_stack((local,np.cross([0,0,1],local),[0,0,1]))
    target_basis = np.column_stack((target,np.cross(desired_normal,target),desired_normal))
    matrix = np.eye(4); matrix[:3,:3] = target_basis @ source_basis.T
    matrix[:3,3] = (a+b)/2 - matrix[:3,:3] @ ((c+d)/2)
    return matrix_pose(matrix)


def link_residual(link, edges, poses):
    a,b = edges[link['a']],edges[link['b']]
    pa = np.asarray(transformed([a['a'],a['b']],poses[a['face_id']]))
    pb = np.asarray(transformed([b['a'],b['b']],poses[b['face_id']]))
    if link.get('flip',True):
        pb = pb[::-1]
    gap = float(np.max(np.linalg.norm(pa-pb,axis=1)))
    ra,rb = pose_matrix(poses[a['face_id']])[:3,:3],pose_matrix(poses[b['face_id']])[:3,:3]
    axis = (pa[1]-pa[0])/np.linalg.norm(pa[1]-pa[0])
    expected = Rotation.from_rotvec(axis*math.radians(link['angle_deg'])).apply(ra@[0,0,1])
    angle_error = math.degrees(math.acos(float(np.clip(np.dot(expected,rb@[0,0,1]),-1,1))))
    return {'endpoint_gap_mm':gap,'angle_error_deg':angle_error,
            'valid':gap<=TOLERANCE_MM and angle_error<=1.0}


def connected_component(face_id, links, edge_map):
    found = {face_id}
    while True:
        before = len(found)
        for link in links:
            pair = {edge_map[link['a']]['face_id'],edge_map[link['b']]['face_id']}
            if pair & found:
                found |= pair
        if len(found)==before:
            return found


def connect_faces(scene, faces, edge_a, edge_b, angle_deg=90, flip=True):
    edges = {e['id']:e for f in faces for e in f['edges']}; face_map={f['id']:f for f in faces}
    if edge_a not in edges or edge_b not in edges:
        raise ValueError('选择的边不存在，可能图纸已经重新解析。')
    a,b = edges[edge_a],edges[edge_b]
    if a['face_id']==b['face_id']:
        raise ValueError('请选择两个不同结构面的边。')
    if any(face_map[e['face_id']]['units']!='mm' for e in (a,b)):
        raise ValueError('这两个面须先确定 mm/pt 比例，再按尺寸连接。')
    if any(edge_a in (x['a'],x['b']) or edge_b in (x['a'],x['b']) for x in scene['links']):
        raise ValueError('选择的边已有连接，请先解除原连接。')
    result = copy.deepcopy(scene)
    moving = connected_component(b['face_id'],result['links'],edges)
    if a['face_id'] not in moving:
        destination = attached_pose(a,b,result['poses'][a['face_id']],angle_deg,flip)
        delta = pose_matrix(destination) @ np.linalg.inv(pose_matrix(result['poses'][b['face_id']]))
        for fid in moving:
            result['poses'][fid] = matrix_pose(delta @ pose_matrix(result['poses'][fid]))
    link = {'id':digest([edge_a,edge_b])[:16], 'a':edge_a, 'b':edge_b,
            'angle_deg':angle_deg,'flip':bool(flip),'basis':'manual_edge_connection'}
    result['links'].append(link)
    result['active_face_ids'] = list(dict.fromkeys(result['active_face_ids']+[a['face_id'],b['face_id']]))
    return result


def face_intersections(polygons, tolerance):
    failures = []
    for (ida,pa),(idb,pb) in itertools.combinations(polygons.items(),2):
        a,b=np.asarray(pa),np.asarray(pb)
        def frame(p):
            u=p[1]-p[0];u/=np.linalg.norm(u)
            n=np.cross(u,p[2]-p[0]);n/=np.linalg.norm(n)
            return u,np.cross(n,u),n
        ua,va,na=frame(a);ub,vb,nb=frame(b)
        if abs(np.dot(na,nb))>.99999 and np.max(np.abs((b-a[0])@na))<tolerance:
            p1=Polygon(np.column_stack(((a-a[0])@ua,(a-a[0])@va)))
            p2=Polygon(np.column_stack(((b-a[0])@ua,(b-a[0])@va)))
            if p1.intersection(p2).area>tolerance**2:
                failures.append({'a':ida,'b':idb,'kind':'coplanar_overlap'})
            continue
        hit=False
        for p,q,u,v,n in ((a,b,ub,vb,nb),(b,a,ua,va,na)):
            interior=Polygon(np.column_stack(((q-q[0])@u,(q-q[0])@v))).buffer(-tolerance)
            for start,end in zip(p,np.roll(p,-1,axis=0)):
                den=np.dot(end-start,n)
                if abs(den)<1e-9:continue
                t=np.dot(q[0]-start,n)/den
                if -.000001<=t<=1.000001:
                    point=start+t*(end-start)-q[0]
                    if interior.contains(Point(np.dot(point,u),np.dot(point,v))):hit=True
        if hit:failures.append({'a':ida,'b':idb,'kind':'face_intersection'})
    return failures


def assess_scene(faces, scene, parallel=()):
    lookup={f['id']:f for f in faces}; edges={e['id']:e for f in faces for e in f['edges']}
    active=[lookup[fid] for fid in scene['active_face_ids'] if fid in lookup]
    polygons={f['id']:transformed(f['polygon'],scene['poses'][f['id']]) for f in active if len(f['polygon'])>=3 and f['units']=='mm'}
    points=[];counts=Counter();edge_faces=defaultdict(set);face_vertices={}
    for fid,polygon in polygons.items():
        ids=[]
        for p in polygon:
            match=next((i for i,q in enumerate(points) if math.dist(p,q)<=TOLERANCE_MM),None)
            if match is None:match=len(points);points.append(p)
            ids.append(match)
        face_vertices[fid]=ids
        for a,b in zip(ids,ids[1:]+ids[:1]):
            if a==b:continue
            key=tuple(sorted((a,b)));counts[key]+=1;edge_faces[key].add(fid)
    intersections=face_intersections(polygons,TOLERANCE_MM)
    residuals=[{**link,**link_residual(link,edges,scene['poses'])} for link in scene['links']]
    graph=defaultdict(set)
    for pair in edge_faces.values():
        if len(pair)==2:
            a,b=pair;graph[a].add(b);graph[b].add(a)
    # Only paired polygon boundaries establish enclosure adjacency. A graph
    # cycle through interior braces does not enclose the faces' open borders.
    cycles=set()
    for a in polygons:
        for b in graph[a]:
            for c in graph[b]-{a}:
                for d in (graph[c]&graph[a])-{a,b,c}:
                    cycles.add(tuple(sorted((a,b,c,d))))
    rings=[]
    for cycle in cycles:
        # Four connected faces must occupy distinct planes and enclose nonzero
        # space; four flat/collapsed sheets do not make a crossarm layer.
        pts=np.asarray([p for fid in cycle for p in polygons[fid]])
        if np.linalg.matrix_rank(pts-pts.mean(axis=0),tol=TOLERANCE_MM)<3:continue
        if any({i['a'],i['b']}<=set(cycle) for i in intersections):continue
        rings.append(list(cycle))
    relations=[]
    for pair in parallel:
        a,b=pair['a'],pair['b']
        na=pose_matrix(scene['poses'][a])[:3,:3]@[0,0,1];nb=pose_matrix(scene['poses'][b])[:3,:3]@[0,0,1]
        error=math.degrees(math.acos(float(np.clip(abs(np.dot(na,nb)),0,1))))
        distance=abs(float(np.dot(np.array(scene['poses'][a]['position'])-scene['poses'][b]['position'],na)))
        measured=lookup[a]['units']=='mm' and lookup[b]['units']=='mm'
        relation={**pair,'parallel_error_deg':error,'plane_distance_mm':distance if measured else None,
                  'constraint_kind':'parallel','distinct_planes':measured and distance>TOLERANCE_MM,
                  'satisfied':error<=1 and measured and distance>TOLERANCE_MM}
        rules=scene.get('taper_rules',[])+([scene['taper_rule']] if scene.get('taper_rule') else [])
        taper=next((r for r in rules if {a,b}=={r['a'],r['b']}),None)
        if taper and {a,b}=={taper['a'],taper['b']}:
            from .assembly_fitting import taper_measurement
            measurement=taper_measurement(taper,edges,scene['poses'])
            component=connected_component(a,scene['links'],edges)
            complete_group=set(taper['face_ids'])<=component
            valid_links=all(link['valid'] for link in residuals
                            if edges[link['a']]['face_id'] in taper['face_ids'] or edges[link['b']]['face_id'] in taper['face_ids'])
            relation.update(constraint_kind='taper',taper=measurement,group_connected=complete_group,
                            satisfied=complete_group and measurement['narrowing'] and valid_links)
        relations.append(relation)
    rings=[ring for ring in rings if all(p['satisfied'] for p in relations if p['a'] in ring and p['b'] in ring)
           and all(link['valid'] for link in residuals if edges[link['a']]['face_id'] in ring and edges[link['b']]['face_id'] in ring)]
    connected=set()
    if polygons:
        todo=[next(iter(polygons))]
        while todo:
            fid=todo.pop()
            if fid in connected:continue
            connected.add(fid);todo.extend(graph[fid]-connected)
    # Orient neighboring faces consistently before summing signed tetrahedra;
    # taking absolute values per triangle would overestimate concave solids.
    orientation={};orientable=True
    for root in polygons:
        if root in orientation:continue
        orientation[root]=1;todo=[root]
        while todo:
            fid=todo.pop();vertices=face_vertices[fid]
            for a,b in zip(vertices,vertices[1:]+vertices[:1]):
                for other in edge_faces[tuple(sorted((a,b)))]-{fid}:
                    ov=face_vertices[other]
                    same=(a,b) in list(zip(ov,ov[1:]+ov[:1]))
                    wanted=orientation[fid]*(-1 if same else 1)
                    if other in orientation:
                        orientable &= orientation[other]==wanted
                    else:orientation[other]=wanted;todo.append(other)
    volume=0.
    if points:
        origin=np.mean(points,axis=0)
        for fid,polygon in polygons.items():
            p=np.asarray(polygon)-origin
            volume+=orientation[fid]*sum(float(np.dot(p[0],np.cross(p[i],p[i+1])))/6 for i in range(1,len(p)-1))
        volume=abs(volume)
    complete=(len(polygons)==len(active) and len(polygons)>=4 and bool(counts) and
              all(n==2 for n in counts.values()) and len(connected)==len(polygons) and
              not intersections and orientable and volume>TOLERANCE_MM**3 and all(r['valid'] for r in residuals) and
              all(p['satisfied'] for p in relations if p['a'] in polygons and p['b'] in polygons))
    status='closed_polyhedron_candidate' if complete else 'lateral_ring_candidate' if rings else 'needs_more_faces_or_manual'
    message={'closed_polyhedron_candidate':'所有参与面的边界已成对闭合；已形成完整多面体候选，仍需核对原图与杆件。',
             'lateral_ring_candidate':'已找到四侧面闭合环；端面或其余边界仍开放。',
             'needs_more_faces_or_manual':'当前未形成可确认的横担层闭合环。请补充上/下/后面等图纸，或在窗口中手动连接。'}[status]
    return {'status':status,'message':message,'lateral_cycles':rings,'watertight':complete,
            'active_faces':len(active),'faces_with_scaled_boundary':len(polygons),
            'open_edges':[{'a':points[a],'b':points[b],'face_ids':sorted(edge_faces[(a,b)])} for (a,b),n in counts.items() if n==1],
            'nonmanifold_edge_count':sum(n>2 for n in counts.values()),'intersections':intersections,
            'euler_characteristic':len(points)-len(counts)+len(polygons),
            'volume_mm3':volume if complete else None,'links':residuals,'parallel_relations':relations,
            'tolerance_mm':TOLERANCE_MM,'engineering_verified':False,
            'missing_or_unscaled_faces':[f['id'] for f in active if f['id'] not in polygons],
            'boundary_note':'面边界来自杆件/板形的外包多边形，需复核；侧面闭合与全部端面闭合分别判断。'}


def auto_assemble(faces, matches, parallel, front_face_id=None):
    """Search four-face cycles using shared marks, then test rigid 90° folds."""
    base=initial_scene(faces);lookup={f['id']:f for f in faces};edges={e['id']:e for f in faces for e in f['edges']}
    if front_face_id in lookup:base['front_face_id']=front_face_id
    adjacency=defaultdict(list)
    for match in matches:
        if not match['automatic_candidate']:continue
        adjacency[match['face_a']].append((match['face_b'],match['a'],match['b']))
        adjacency[match['face_b']].append((match['face_a'],match['b'],match['a']))
    # Do not silently assemble an unrelated ring while leaving the designated
    # front face outside it. The user can choose another front explicitly.
    roots=[base['front_face_id']] if base['front_face_id'] in adjacency else []
    tried=0;limit=3000
    for root in roots:
        for b,ab,ba in adjacency[root]:
            for c,bc,cb in adjacency[b]:
                if c==root or bc==ba:continue
                for d,cd,dc in adjacency[c]:
                    if d in {root,b} or cd==cb:continue
                    closures=[(dr,rd) for other,dr,rd in adjacency[d] if other==root and dr!=dc and rd!=ab]
                    for dr,rd in closures:
                        for choices in itertools.product(((90,True),(90,False),(-90,True),(-90,False)),repeat=3):
                            tried+=1
                            if tried>limit:
                                return {'found':False,'tested':tried-1,'search_limited':True,'scene':base}
                            scene=copy.deepcopy(base);scene['active_face_ids']=[root,b,c,d];scene['front_face_id']=root
                            scene['poses'][root]={'position':[0,0,-min(p[1] for p in lookup[root]['polygon'])], 'quaternion':FRONT_QUATERNION[:]}
                            scene['links']=[]
                            for (ea,eb),(angle,flip) in zip(((ab,ba),(bc,cb),(cd,dc)),choices):
                                scene['poses'][edges[eb]['face_id']]=attached_pose(edges[ea],edges[eb],scene['poses'][edges[ea]['face_id']],angle,flip)
                                scene['links'].append({'id':digest([ea,eb])[:16],'a':ea,'b':eb,'angle_deg':angle,'flip':flip,'basis':'shared_mark_90_degree_candidate'})
                            closing=None
                            for angle,flip in ((90,True),(90,False),(-90,True),(-90,False)):
                                link={'id':digest([dr,rd])[:16],'a':dr,'b':rd,'angle_deg':angle,'flip':flip,'basis':'shared_mark_cycle_closure'}
                                if link_residual(link,edges,scene['poses'])['valid']:closing=link;break
                            if closing is None:continue
                            scene['links'].append(closing)
                            assessment=assess_scene(faces,scene,parallel)
                            if assessment['lateral_cycles'] and not assessment['intersections'] and all(p['satisfied'] for p in assessment['parallel_relations'] if p['a'] in scene['active_face_ids'] and p['b'] in scene['active_face_ids']):
                                scene=attach_supported_faces(faces,matches,parallel,scene)
                                return {'found':True,'tested':tried,'search_limited':False,'scene':scene}
    return {'found':False,'tested':tried,'search_limited':False,'scene':base}


def attach_supported_faces(faces, matches, parallel, scene):
    """Extend a closed side ring only when two distinct shared edges agree.

    This can add end caps or further supported faces. One hinge alone leaves
    placement ambiguous and is deliberately left for the manual workspace.
    """
    edges={e['id']:e for f in faces for e in f['edges']}
    strong=[m for m in matches if m['automatic_candidate']]
    for _ in faces:
        active=set(scene['active_face_ids']);used={e for link in scene['links'] for e in (link['a'],link['b'])}
        candidates=defaultdict(list)
        for match in strong:
            for a,b in ((match['a'],match['b']),(match['b'],match['a'])):
                if edges[a]['face_id'] in active and edges[b]['face_id'] not in active and a not in used and b not in used:
                    candidates[edges[b]['face_id']].append((a,b))
        best=None;best_count=0
        for fid,pairs in candidates.items():
            if len(pairs)<2:continue
            for ea,eb in pairs:
                for angle,flip in ((90,True),(90,False),(-90,True),(-90,False)):
                    trial=copy.deepcopy(scene)
                    trial['poses'][fid]=attached_pose(edges[ea],edges[eb],scene['poses'][edges[ea]['face_id']],angle,flip)
                    accepted=[];taken=set()
                    for a,b in pairs:
                        if a in taken or b in taken:continue
                        for an,fl in ((90,True),(90,False),(-90,True),(-90,False)):
                            link={'id':digest([a,b])[:16],'a':a,'b':b,'angle_deg':an,'flip':fl,'basis':'two_shared_edges_candidate'}
                            if link_residual(link,edges,trial['poses'])['valid']:
                                accepted.append(link);taken.update((a,b));break
                    if len(accepted)<2:continue
                    trial['active_face_ids'].append(fid);trial['links']+=accepted
                    report=assess_scene(faces,trial,parallel)
                    relevant=set(trial['active_face_ids'])
                    if report['intersections'] or report['nonmanifold_edge_count'] or any(not p['satisfied'] for p in report['parallel_relations'] if p['a'] in relevant and p['b'] in relevant):continue
                    if len(accepted)>best_count:best,best_count=trial,len(accepted)
        if best is None:break
        scene=best
    return scene
