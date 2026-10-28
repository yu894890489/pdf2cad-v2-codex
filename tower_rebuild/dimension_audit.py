"""Preliminary dimensional cross-checks, independent of BOM-fitted extents.

Small dimension polygons specify spans, not necessarily stock lengths. Only
native label values and original line directions enter these calculations.
Source values are never changed by an audit recommendation.
"""
import itertools
import math
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import fitz
import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree
from shapely.geometry import Polygon

from .io import read_json, write_json, file_digest, digest
from .topology_geometry import unit_segment, point_segment_distance, native_strokes


def midpoint(box):
    return np.array([(box[0]+box[2])/2,(box[1]+box[3])/2])


def precision(text):
    return 10.**(-len(text.split('.')[1])) if '.' in text else 1.


def text_geometry(primitives,source):
    docs=fitz.open(source);result={}
    layouts={i+1:p.get_text('dict')['blocks'] for i,p in enumerate(docs)}
    for p in primitives:
        if p['primitive_type']!='text':continue
        b=layouts[p['page']][p['text_block']]
        line=b['lines'][p['text_line']]
        result[p['id']]={**p,'direction':line['dir'],'font_size_pt':max(s['size'] for s in line['spans'])}
    docs.close()
    return result


def dimension_polygons(view,geometry,texts,excluded):
    """Closed isolated thin-line cycles, with labels assigned once per view."""
    strokes=[s for s in geometry['strokes'] if s['width_pt']<=.62 and 1.2<=s['length_pt']<=180]
    if not strokes:return []
    pts=np.array([p for s in strokes for p in (s['a'],s['b'])]);parent=list(range(len(pts)))
    def root(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for a,b in cKDTree(pts).query_pairs(.25):parent[root(b)]=root(a)
    nodes=defaultdict(list)
    for i,p in enumerate(pts):nodes[root(i)].append(p)
    xy={n:np.mean(v,axis=0) for n,v in nodes.items()};adj=defaultdict(list)
    for i in range(len(strokes)):
        a,b=root(2*i),root(2*i+1)
        if a!=b:adj[a].append((b,i));adj[b].append((a,i))
    visited=set();polygons=[]
    for start in adj:
        if start in visited:continue
        stack=[start];component=set()
        while stack:
            x=stack.pop()
            if x in component:continue
            component.add(x);stack.extend(n for n,_ in adj[x])
        visited|=component
        if not 3<=len(component)<=8 or any(len(adj[n])!=2 for n in component):continue
        order=[];edges=[];current=start;previous=None
        while current not in order:
            order.append(current);n,i=next((n,i) for n,i in adj[current] if n!=previous)
            edges.append(strokes[i]);previous,current=current,n
        poly=Polygon([xy[n] for n in order])
        if current!=start or not poly.is_valid or not 20<poly.area<16000:continue
        if max(poly.bounds[2]-poly.bounds[0],poly.bounds[3]-poly.bounds[1])>190:continue
        polygons.append({'id':f"{view['id']}_dimensions_{len(polygons)+1:02d}",'view_id':view['id'],'page':view['page'],
                         'vertices_pt':[xy[n].tolist() for n in order], 'bbox_pt':list(poly.bounds),
                         'edges':[{'a':xy[order[j]].tolist(),'b':xy[order[(j+1)%len(order)]].tolist(),
                                   'primitive_ids':s['primitive_ids']} for j,s in enumerate(edges)]})
    labels=[t for t in texts if t['id'] not in excluded and re.fullmatch(r'\d+(?:\.\d+)?',t['text']) and float(t['text'])>0]
    edges=[e for p in polygons for e in p['edges']]
    if not edges or not labels:return []
    costs=np.full((len(edges),len(labels)+len(edges)),1000.)
    for i,e in enumerate(edges):
        a,b=np.array(e['a']),np.array(e['b']);u=(b-a)/np.linalg.norm(b-a)
        for j,t in enumerate(labels):
            alignment=abs(float(u@np.array(t['direction'])))
            if alignment<math.cos(math.radians(12)):continue
            distance,along=point_segment_distance(midpoint(t['bbox_pt']),a,b)
            if distance>max(13,t['font_size_pt']*2.2):continue
            costs[i,j]=distance+math.degrees(math.acos(min(1,alignment)))*.25+max(0,-along,along-1)*np.linalg.norm(b-a)*.3
    ii,jj=linear_sum_assignment(costs)
    for i,j in zip(ii,jj):
        if j>=len(labels) or costs[i,j]>=1000:continue
        t=labels[j];edges[i].update(value_mm=float(t['text']),text=t['text'],text_id=t['id'],label_bbox_pt=t['bbox_pt'],rounding_mm=precision(t['text'])/2)
    return [p for p in polygons if sum('value_mm' in e for e in p['edges'])>=max(3,len(p['edges'])-1)]


def projected_axes(members):
    groups=[]
    for m in members:
        axis=m.get('visible_axis') or m.get('axis')
        if not axis:continue
        a,b,u,length=unit_segment(axis['a'],axis['b'])
        group=next((g for g in groups if abs(float(u@np.array(g['u'])))>math.cos(math.radians(.3))
                    and abs(float(np.cross(np.array(g['u']),a-np.array(g['a']))))<.8),None)
        if group is None:
            group={'id':f"{m['view_id']}_axis_{len(groups)+1:02d}",'a':a.tolist(),'b':b.tolist(),'u':u.tolist(),'member_ids':[],'marks':[],'native_segments':[]};groups.append(group)
        group['member_ids'].append(m['id']);group['marks'].append(m['mark'])
        group['native_segments'].append({'a':a.tolist(),'b':b.tolist(),'member_id':m['id'],'basis':'original_visible_axis'})
        for extension in m.get('overlap_recovery',{}).get('extensions',[]):
            # Include only the actual solid continuation intervals. Inferred gaps,
            # target BOM extents and hidden-only tracks are not independent ink.
            ids=extension.get('native_edge_primitive_ids',[])
            if not ids or extension.get('hidden_edge_primitive_ids'):continue
            for lo,hi in extension.get('visible_edge_intervals_pt',[]):
                group['native_segments'].append({'a':(a+lo*u).tolist(),'b':(a+hi*u).tolist(),
                    'member_id':m['id'],'primitive_ids':ids,'basis':'native_solid_edge_continuation'})
    return groups


def native_face_support(combination,vertices):
    """Require actual ink inside each proposed face side, not an infinite line.

    This is a correspondence filter in PDF coordinates, not a length tolerance
    or an assertion that a cut endpoint equals a theoretical intersection.
    Disjoint observed intervals remain disjoint; no gap is filled here.
    """
    evidence=[]
    for i,g in enumerate(combination):
        u=np.array(g['u']);lo,hi=sorted(float(np.array(p)@u) for p in (vertices[i],vertices[(i+1)%len(vertices)]))
        overlaps=[];sources=[]
        for s in g['native_segments']:
            start,end=sorted(float(np.array(s[k])@u) for k in ('a','b'))
            left,right=max(lo,start),min(hi,end)
            if right>left:overlaps.append((left,right));sources.append(s)
        merged=[]
        for left,right in sorted(overlaps):
            if merged and left<=merged[-1][1]:merged[-1][1]=max(right,merged[-1][1])
            else:merged.append([left,right])
        covered=sum(right-left for left,right in merged)
        evidence.append({'axis_id':g['id'],'overlap_pt':covered,'side_length_pt':hi-lo,
                         'overlap_fraction':covered/(hi-lo),'has_native_overlap':covered>.25,'sources':sources})
    return {'all_edges_supported':all(e['has_native_overlap'] for e in evidence),'edges':evidence,
            'basis':'original_visible_segments_and_native_solid_continuations_only',
            'contact_tolerance_pt':.25,'engineering_verified':False}


def discover_polygons(primitives, texts, views, members, excluded):
    """Detect before partitioning: a view boundary must not cut a closed sketch."""
    ownership={pid:v['id'] for v in views for pid in v['primitive_ids']}
    result=[];counts=Counter()
    for page in sorted({v['page'] for v in views}):
        ink=native_strokes([{**p,'role':'unassigned_geometry'} if p['role']=='dash_segment_candidate' else p
                            for p in primitives if p['page']==page and p.get('part')=='structure'])
        polygons=dimension_polygons({'id':f'p{page}','page':page},{'strokes':ink},
                                    [t for t in texts.values() if t['page']==page and t.get('part')=='structure'],excluded)
        for poly in polygons:
            ids=[pid for e in poly['edges'] for pid in e['primitive_ids']]+[e['text_id'] for e in poly['edges'] if 'text_id' in e]
            votes=Counter(ownership[pid] for pid in ids if pid in ownership)
            options=votes.most_common();basis='majority_of_native_primitives'
            if options and (len(options)==1 or options[0][1]>options[1][1]):vid=options[0][0]
            else:
                center=midpoint(poly['bbox_pt']);distances=[]
                for view in views:
                    if view['page']!=page:continue
                    b=view['bbox_pt']
                    if not (b[0]<=center[0]<=b[2] and b[1]<=center[1]<=b[3]):continue
                    distances.extend((point_segment_distance(center,*[(m.get('visible_axis') or m['axis'])[k] for k in ('a','b')])[0],view['id'])
                                     for m in members if m['view_id']==view['id'] and m.get('axis'))
                if not distances:vid=f'p{page}_unassigned'
                else:vid=min(distances)[1]
                basis='nearest_member_in_containing_view_candidate' if distances else 'unassigned'
            counts[vid]+=1
            poly.update(id=f'{vid}_dimensions_{counts[vid]:02d}',view_id=vid,view_assignment_basis=basis,
                        previously_unassigned_primitive_ids=[pid for pid in ids if pid not in ownership])
            result.append(poly)
    return result


def end_allowances(edge, texts):
    """Signed parallel labels near both ends are candidates, never assumed standards."""
    a,b=np.array(edge['structural_a_pt']),np.array(edge['structural_b_pt']);u=(b-a)/np.linalg.norm(b-a)
    limit=min(40.,np.linalg.norm(b-a)*.25);ends=[]
    for point in (a,b):
        candidates=[]
        for t in texts:
            raw=t['text'].replace('−','-').replace('＋','+')
            if not re.fullmatch(r'[+-]\d+(?:\.\d+)?',raw):continue
            if abs(float(np.array(t['direction'])@u))<math.cos(math.radians(5)):continue
            delta=midpoint(t['bbox_pt'])-point
            distance=float(np.linalg.norm(delta));normal=abs(float(u[0]*delta[1]-u[1]*delta[0]))
            if distance<=limit and normal<=20:
                candidates.append({'text_id':t['id'],'value_mm':float(raw),'rounding_mm':precision(raw)/2,
                                   'bbox_pt':t['bbox_pt'],'distance_pt':distance})
        ends.append(sorted(candidates,key=lambda c:c['distance_pt']))
    unique=all(len(c)==1 for c in ends) and ends[0][0]['text_id']!=ends[1][0]['text_id']
    return {'status':'two_end_label_candidates' if unique else 'missing_or_ambiguous_end_basis','end_candidates':ends,
            'selected':[c[0] for c in ends] if unique else []}


def adjacent_span_extensions(edge, polygon, polygons):
    """Follow a labelled common triangle side to a collinear end segment.

    This uses the diagram's connectivity and side directions for correspondence,
    never the BOM length to choose an extension that would make it fit.
    """
    extensions=[];ambiguous=False
    for endpoint,node in enumerate(edge['node_ids']):
        neighbor=next(e for e in polygon['edges'] if e is not edge and node in e.get('node_ids',[]))
        if 'value_mm' not in neighbor:continue
        reference=np.array(neighbor['b'],float)-neighbor['a'];reference/=np.linalg.norm(reference)
        outward=np.array(edge['direction'])*(1 if endpoint==1 else -1)
        reference_endpoint=neighbor['node_ids'].index(node);candidates=[]
        for other in polygons:
            if other['id']==polygon['id'] or other['view_id']!=polygon['view_id']:continue
            for index,shared in enumerate(other['edges']):
                if 'value_mm' not in shared or abs(shared['value_mm']-neighbor['value_mm'])>.01:continue
                direction=np.array(shared['b'],float)-shared['a'];direction/=np.linalg.norm(direction)
                alignment=float(direction@reference)
                if abs(alignment)<math.cos(math.radians(1)):continue
                at=reference_endpoint if alignment>0 else 1-reference_endpoint
                extension=other['edges'][(index+1 if at else index-1)%len(other['edges'])]
                ray=np.array(extension['b'],float)-extension['a'];ray/=np.linalg.norm(ray)
                if not at:ray=-ray
                if float(ray@outward)<math.cos(math.radians(1)) or 'value_mm' not in extension:continue
                # The inset must lie close to this endpoint within the same view.
                location=np.array(edge['structural_b_pt'] if endpoint else edge['structural_a_pt'])
                if np.linalg.norm(midpoint(other['bbox_pt'])-location)>180:continue
                candidates.append({'endpoint':endpoint,'value_mm':extension['value_mm'],'rounding_mm':extension['rounding_mm'],
                                   'text_id':extension['text_id'],'polygon_id':other['id'],
                                   'shared_side_text_ids':[neighbor['text_id'],shared['text_id']],
                                   'basis':'same_labelled_common_side_and_collinear_outward_segment'})
        # Repeated projections of the same value are evidence, never additive.
        unique={c['value_mm'] for c in candidates}
        if len(unique)==1:extensions.append({**candidates[0],'supporting_text_ids':[c['text_id'] for c in candidates]})
        elif len(unique)>1:ambiguous=True
    return {'segments':extensions,'ambiguous':ambiguous}


def span_bom_checks(polygons,texts_by_view,members):
    """Compare only with explicit signed end labels; partial spans remain pending."""
    result=[];seen=set();member_map={m['id']:m for m in members}
    for poly in polygons:
        if poly['association_status']!='projected_face_candidate':continue
        for edge in poly['edges']:
            if 'value_mm' not in edge:continue
            for mid in edge['member_ids']:
                key=(mid,tuple(sorted(edge['node_ids'])),edge['value_mm'])
                if key in seen:continue
                seen.add(key);m=member_map[mid];basis=end_allowances(edge,texts_by_view[poly['view_id']])
                extensions=adjacent_span_extensions(edge,poly,polygons)
                check={'id':f'span_{len(result)+1:03d}','view_id':poly['view_id'],'polygon_id':poly['id'],
                       'member_id':mid,'mark':m['mark'],'span_mm':edge['value_mm'],'span_text_id':edge['text_id'],
                       'bom_length_mm':m['bom']['length_mm'],'end_basis':basis,'span_extensions':extensions,'status':'basis_incomplete',
                       'pending_reasons':[],
                       'assumption':'尺寸边对应整根杆件的两端节点，正负标注分别是相同节点基准的伸长/退距；对应关系仍为候选。'}
                if basis['selected'] and not extensions['ambiguous']:
                    terms=extensions['segments']+basis['selected']
                    length=edge['value_mm']+sum(c['value_mm'] for c in terms)
                    delta=length-m['bom']['length_mm'];tolerance=edge['rounding_mm']+sum(c['rounding_mm'] for c in terms)+.5
                    check.update(computed_cut_length_mm=length,delta_mm=delta,rounding_bound_mm=tolerance,
                                 formula=f"{edge['value_mm']:g}"+''.join(f" {c['value_mm']:+g}" for c in terms)+f" = {length:g} mm",
                                 status='consistent_given_end_basis' if abs(delta)<=tolerance else 'end_basis_or_length_conflict')
                else:
                    for end,candidates in enumerate(basis['end_candidates']):
                        name='起点' if end==0 else '终点'
                        if not candidates:check['pending_reasons'].append({'code':f'end_{end}_missing','message':f'{name}未找到可对应的正负尺寸'})
                        elif len(candidates)>1:check['pending_reasons'].append({'code':f'end_{end}_ambiguous','message':f'{name}有 {len(candidates)} 个正负标注候选'})
                    if all(len(c)==1 for c in basis['end_candidates']) and not basis['selected']:
                        check['pending_reasons'].append({'code':'same_label_at_both_ends','message':'两端候选指向同一标注，不能重复使用'})
                    if extensions['ambiguous']:check['pending_reasons'].append({'code':'extension_ambiguous','message':'相邻端段长度存在多个候选'})
                result.append(check)
    return result


def summarize_views(views,polygons,spans,annotations,findings):
    """No findings is not a pass when a face has missing evidence or coverage."""
    result=[]
    for view in views:
        selected=lambda records:[r for r in records if r['view_id']==view['id']]
        ps,ss,aa,ff=map(selected,(polygons,spans,annotations,findings))
        counts={'dimension_polygons':len(ps),'direction_consistent':sum(p.get('closure',{}).get('status')=='consistent_with_rounding' for p in ps),
                'direction_conflicts':sum(p.get('closure',{}).get('status')=='directional_inconsistency' for p in ps),
                'correspondence_pending':sum(p['association_status']!='projected_face_candidate' or not p['labels_complete'] for p in ps),
                'native_support_resolved':sum(p.get('native_support_resolved_ambiguity',False) for p in ps),
                'span_bom_checked':sum('computed_cut_length_mm' in s for s in ss),
                'span_bom_consistent':sum(s['status']=='consistent_given_end_basis' for s in ss),
                'span_bom_conflicts':sum(s['status']=='end_basis_or_length_conflict' for s in ss),
                'span_basis_pending':sum(s['status']=='basis_incomplete' for s in ss),
                'inline_bom_checks':len(aa),'inline_comparison_consistent':sum(a['status']=='consistent' for a in aa),
                'inline_correspondence_pending':sum(a['association_status']=='ambiguous' or not a['outline_direction_agrees'] for a in aa),
                'findings':len(ff)}
        if ff:status='conflicts_to_review';conclusion='存在初审矛盾，见候选计算与原图'
        elif not ps:status='no_dimension_diagram';conclusion='未检出可计算尺寸图'+('；仅有杆旁标注对照' if aa else '；尚无尺寸验算结论')
        elif counts['correspondence_pending'] or counts['span_basis_pending'] or counts['inline_correspondence_pending']:
            status='evidence_incomplete';conclusion='已完成的计算未见矛盾；其余依据不足'
        else:status='conditional_checks_only';conclusion='现有计算未见矛盾；对应与端部基准仍需复核'
        result.append({'view_id':view['id'],'title':view['title'],'counts':counts,'status':status,'conclusion':conclusion,
                       'finding_ids':[f['id'] for f in ff],'engineering_verified':False})
    return result


def intersection(first,second):
    a,u=np.array(first['a']),np.array(first['u']);b,v=np.array(second['a']),np.array(second['u'])
    matrix=np.column_stack((u,-v))
    if abs(np.linalg.det(matrix))<.04:return None
    return a+u*np.linalg.solve(matrix,b-a)[0]


def associate_polygon(poly,axes,view):
    choices=[];limit=max(90,min(240,(view['bbox_pt'][2]-view['bbox_pt'][0])*.3))
    for e in poly['edges']:
        a,b=np.array(e['a']),np.array(e['b']);u=(b-a)/np.linalg.norm(b-a);c=(a+b)/2
        options=[]
        for g in axes:
            alignment=abs(float(u@np.array(g['u'])))
            distance=abs(float(np.cross(np.array(g['u']),c-np.array(g['a']))))
            if alignment>math.cos(math.radians(8)) and distance<limit:
                options.append((distance+100*(1-alignment),g))
        choices.append([g for _,g in sorted(options,key=lambda v:v[0])[:3]])
    candidates=[];center=Polygon(poly['vertices_pt']).centroid
    if all(choices):
        for combination in itertools.product(*choices):
            if len({g['id'] for g in combination})!=len(combination):continue
            vertices=[intersection(combination[i-1],g) for i,g in enumerate(combination)]
            if any(p is None for p in vertices):continue
            footprint=Polygon(vertices)
            if not footprint.is_valid or not footprint.buffer(5).covers(center):continue
            directions=[];score=0;valid=True
            for i,e in enumerate(poly['edges']):
                direction=vertices[(i+1)%len(vertices)]-vertices[i];length=np.linalg.norm(direction)
                if length<5:valid=False;break
                direction/=length;drawn=np.array(e['b'],float)-e['a'];drawn/=np.linalg.norm(drawn)
                alignment=float(direction@drawn)
                if alignment<math.cos(math.radians(8)):valid=False;break
                directions.append(direction.tolist());score+=100*(1-alignment)
            if valid:candidates.append((score,combination,vertices,directions))
    candidates.sort(key=lambda v:v[0])
    if not candidates:
        poly['association_status']='unresolved';return
    # Do not promote a worse-angle candidate simply because it has more ink.
    competitive=[c for c in candidates if c[0]-candidates[0][0]<.15]
    supports=[native_face_support(c[1],c[2]) for c in competitive]
    supported=[i for i,s in enumerate(supports) if s['all_edges_supported']]
    chosen=supported[0] if len(supported)==1 else 0
    score,combination,vertices,directions=competitive[chosen]
    status='projected_face_candidate' if len(supported)==1 else 'ambiguous' if supported else 'unresolved_native_extent'
    poly.update(association_status=status,
                structural_vertices_pt=[p.tolist() for p in vertices],direction_source='original_member_parallel_outline_directions_not_bom_fitted_lengths',
                association_basis='unique_native_supported_angular_candidate' if len(supported)==1 else 'correspondence_pending',
                native_support=supports[chosen],native_support_resolved_ambiguity=len(competitive)>1 and len(supported)==1,
                angular_candidate_count=len(competitive),native_supported_candidate_count=len(supported),
                association_alternatives=[{'axis_ids':[g['id'] for g in c[1]],'score':c[0],
                    'all_edges_supported':s['all_edges_supported'],'overlap_fractions':[e['overlap_fraction'] for e in s['edges']]}
                    for c,s in zip(competitive,supports)])
    node_ids=['|'.join(sorted((combination[i-1]['id'],g['id']))) for i,g in enumerate(combination)]
    for i,e in enumerate(poly['edges']):
        g=combination[i];e.update(axis_id=g['id'],member_ids=g['member_ids'],marks=g['marks'],direction=directions[i],
                                 node_ids=[node_ids[i],node_ids[(i+1)%len(node_ids)]],
                                 structural_a_pt=vertices[i].tolist(),structural_b_pt=vertices[(i+1)%len(vertices)].tolist())


def vector_check(values,directions,rounding):
    """A conditional direction-closure check; not a right-angle assumption."""
    values=np.array(values,float);directions=np.array(directions,float)
    residual=values@directions;tolerance=max(1.,sum(rounding));alternatives=[]
    for i in range(len(values)):
        others=residual-values[i]*directions[i];value=-float(others@directions[i]);step=rounding[i]*2
        rounded=round(value/step)*step;error=float(np.linalg.norm(others+rounded*directions[i]))
        if value<=0:continue
        alternatives.append({'edge_index':i,'computed_value_mm':value,'rounded_candidate_mm':rounded,
                             'change_mm':rounded-values[i],'residual_after_mm':error})
    alternatives.sort(key=lambda a:a['residual_after_mm'])
    accepted=[a for a in alternatives if a['residual_after_mm']<=tolerance and abs(a['change_mm'])>rounding[a['edge_index']]*2]
    recommendation=accepted[0] if len(accepted)==1 else None
    return {'residual_xy_mm':residual.tolist(),'residual_norm_mm':float(np.linalg.norm(residual)),
            'rounding_bound_mm':tolerance,'status':'consistent_with_rounding' if np.linalg.norm(residual)<=tolerance else 'directional_inconsistency',
            'single_value_alternatives':alternatives,'recommendation':recommendation,
            'assumption':'当前结构面是等比例平面投影，所配杆件方向及尺寸示意图对应关系正确；不使用示意图画出的角度作为精确角度。'}


def cosine_triangle(values):
    a,b,c=values
    if min(values)<=0 or max(values)>=sum(values)-max(values):return {'status':'triangle_inequality_failed'}
    angles=[math.degrees(math.acos(np.clip((x*x+y*y-z*z)/(2*x*y),-1,1))) for x,y,z in ((b,c,a),(a,c,b),(a,b,c))]
    return {'status':'valid_triangle','angles_opposite_edges_deg':angles,'right_angle_assumed':False}


def cosine_span_interval(values, rounding):
    """Conservative interval arithmetic, only when the positive root is unique."""
    (bl,bh),(yl,yh),(sl,sh),(tl,th)=[(v-r,v+r) for v,r in zip(values,rounding)]
    if min(bl,yl,sl,tl)<=0 or sl<=bh:return None  # SSA can have two positive roots.
    b,y,s,t=values;c=(b*b+y*y-t*t)/(2*b*y)
    if not -1<=c<=1:return None
    nominal=-b*c+math.sqrt(s*s-b*b*(1-c*c))
    numerator=(bl*bl+yl*yl-th*th,bh*bh+yh*yh-tl*tl)
    denominator=(2*bl*yl,2*bh*yh)
    ratios=[n/d for n in numerator for d in denominator]
    cl,ch=max(-1.,min(ratios)),min(1.,max(ratios))
    square_min=0. if cl<=0<=ch else min(cl*cl,ch*ch)
    square_max=max(cl*cl,ch*ch)
    rad_lo=max(0.,sl*sl-bh*bh*(1-square_min));rad_hi=sh*sh-bl*bl*(1-square_max)
    shift=[-bb*cc for bb in (bl,bh) for cc in (cl,ch)]
    return nominal,[min(shift)+math.sqrt(rad_lo),max(shift)+math.sqrt(rad_hi)]


def straight_triangle_check(first,second):
    """Law of cosines across two triangles sharing an edge and straight brace."""
    checks=[]
    if len(first['edges'])!=3 or len(second['edges'])!=3:return checks
    if any('node_ids' not in e or 'value_mm' not in e for e in first['edges']+second['edges']):return checks
    for common in first['edges']:
        other=next((e for e in second['edges'] if e['axis_id']==common['axis_id'] and set(e['node_ids'])&set(common['node_ids'])),None)
        if not other or abs(common['value_mm']-other['value_mm'])>common['rounding_mm']+other['rounding_mm']:continue
        endpoint_distance=min(max(math.dist(common['structural_a_pt'],other['structural_a_pt']),math.dist(common['structural_b_pt'],other['structural_b_pt'])),
                              max(math.dist(common['structural_a_pt'],other['structural_b_pt']),math.dist(common['structural_b_pt'],other['structural_a_pt'])))
        if endpoint_distance>15:continue
        for pivot in set(common['node_ids'])&set(other['node_ids']):
            left=next(e for e in first['edges'] if e is not common and pivot in e['node_ids'])
            right=next(e for e in second['edges'] if e is not other and pivot in e['node_ids'])
            if left['axis_id']!=right['axis_id']:continue
            # Verify opposite rays, not two labels on the same side of a joint.
            ld=np.array(left['direction'])*(1 if left['node_ids'][0]==pivot else -1)
            rd=np.array(right['direction'])*(1 if right['node_ids'][0]==pivot else -1)
            if float(ld@rd)>-.999:continue
            side1=next(e for e in first['edges'] if pivot not in e['node_ids']);side2=next(e for e in second['edges'] if pivot not in e['node_ids'])
            b,y,s,t=[e['value_mm'] for e in (common,right,side1,side2)]
            solved=cosine_span_interval([b,y,s,t],[e['rounding_mm'] for e in (common,right,side1,side2)])
            if solved is None:continue
            value,bounds=solved
            checks.append({'polygon_ids':[first['id'],second['id']],'target_text_id':left['text_id'],'observed_mm':left['value_mm'],
                           'computed_mm':value,'rounding_interval_mm':bounds,'range_method':'conservative_interval_arithmetic_unique_positive_root',
                           'input_values_mm':{'shared_side':b,'opposite_segment':y,'first_outer_side':s,'second_outer_side':t},
                           'input_text_ids':[e['text_id'] for e in (common,right,side1,side2)],
                           'input_rounding_mm':[e['rounding_mm'] for e in (common,right,side1,side2)],'target_rounding_mm':left['rounding_mm'],
                           'shared_node_projection_deviation_pt':endpoint_distance,
                           'formula':'c=(b²+y²-t²)/(2by); x=-bc+√(s²-b²(1-c²))',
                           'assumption':'两个示意三角形共享该边，另两边属于同一根直杆且位于交点两侧；未假定直角。'})
    return checks


def compare_dimension_hypotheses(polygon, spans, triangle_checks):
    """Compare proposed edits against all available constraints, without editing inputs.

    A small directional residual alone cannot outrank a contradictory BOM or a
    neighbouring dimension constraint. Shared inputs are recalculated for each
    hypothesis, so changing a side cannot reuse the old cosine result as proof.
    """
    closure=polygon['closure'];edge_by_text={e['text_id']:e for e in polygon['edges']}
    hypotheses=[{'changed_text_id':None,'original_mm':None,'candidate_mm':None,
                 'residual_mm':closure['residual_norm_mm'],'label':'保留原标注'}]
    for alternative in closure['single_value_alternatives']:
        edge=polygon['edges'][alternative['edge_index']];value=alternative['rounded_candidate_mm']
        if abs(value-edge['value_mm'])<1e-8:continue
        hypotheses.append({'changed_text_id':edge['text_id'],'original_mm':edge['value_mm'],'candidate_mm':value,
                           'residual_mm':alternative['residual_after_mm'],
                           'label':f"{edge['value_mm']:g} → {value:g} mm"})
    anchors=[]
    for span in spans:
        # A computed disagreement is a constraint too; keeping only original
        # matches would hide contrary evidence when selecting a correction.
        if 'computed_cut_length_mm' not in span:continue
        input_ids={span['span_text_id']}|{e['text_id'] for e in span['span_extensions']['segments']}
        if input_ids&edge_by_text.keys():anchors.append((span,input_ids))
    checks=[t for t in triangle_checks if ({t['target_text_id']}|set(t['input_text_ids']))&edge_by_text.keys()]
    for hypothesis in hypotheses:
        changed=hypothesis['changed_text_id'];value=hypothesis['candidate_mm']
        delta=value-hypothesis['original_mm'] if changed else 0.
        sides=[value if e['text_id']==changed else e['value_mm'] for e in polygon['edges']]
        hypothesis['side_lengths_valid']=bool(min(sides)>0 and max(sides)<sum(sides)-max(sides))
        hypothesis['direction_passed']=hypothesis['residual_mm']<=closure['rounding_bound_mm']
        hypothesis['bom_checks']=[];hypothesis['triangle_checks']=[]
        for span,input_ids in anchors:
            computed=span['computed_cut_length_mm']+(delta if changed in input_ids else 0.)
            difference=computed-span['bom_length_mm']
            hypothesis['bom_checks'].append({'span_check_id':span['id'],'mark':span['mark'],
                'computed_cut_length_mm':computed,'bom_length_mm':span['bom_length_mm'],'delta_mm':difference,
                'passed':abs(difference)<=span['rounding_bound_mm'],
                'formula_basis':span['formula'],'changed_input_in_formula':changed in input_ids,
                'input_text_ids':sorted(input_ids|{c['text_id'] for c in span['end_basis']['selected']})})
        for check in checks:
            # Ordered inputs: shared side, opposite segment, first and second outer sides.
            keys=('shared_side','opposite_segment','first_outer_side','second_outer_side')
            values=[value if tid==changed else check['input_values_mm'][key] for key,tid in zip(keys,check['input_text_ids'])]
            radii=check.get('input_rounding_mm',[.5]*4)
            solved=cosine_span_interval(values,radii)
            target=value if changed==check['target_text_id'] else check['observed_mm']
            radius=check.get('target_rounding_mm',edge_by_text.get(check['target_text_id'],{}).get('rounding_mm',.5))
            result={'target_text_id':check['target_text_id'],'target_mm':target,'input_values_mm':values,
                    'input_text_ids':check['input_text_ids'],'recomputed_for_hypothesis':True}
            if solved:
                nominal,bounds=solved
                result.update(computed_mm=nominal,interval_mm=bounds,passed=target+radius>=bounds[0] and target-radius<=bounds[1])
            else:result.update(passed=None,reason='当前假设下余弦反算无唯一可用解')
            hypothesis['triangle_checks'].append(result)
        hypothesis['all_available_checks_passed']=hypothesis['side_lengths_valid'] and hypothesis['direction_passed'] and all(c['passed'] is True for c in hypothesis['bom_checks']+hypothesis['triangle_checks'])
    eligible=[h for h in hypotheses if h['changed_text_id'] and h['all_available_checks_passed']]
    preferred=eligible[0] if len(eligible)==1 else None
    status=('cross_checked_candidate' if preferred and (anchors or checks) else 'direction_only_candidate') if preferred else 'underdetermined'
    return {'status':status,'hypotheses':hypotheses,'preferred':preferred,'bom_anchor_count':len(anchors),'adjacent_triangle_check_count':len(checks),
            'selection_rule':'单值候选必须同时通过可用的方向闭合、相邻尺寸反算和 BOM 换算；没有唯一候选则待定。',
            'conditions':['结构面的投影与尺寸边/杆件对应正确。','BOM 换算中的分段跨度和端部正负尺寸属于同一基准。',
                          '相邻验算共享部分原始尺寸，不能当作完全独立的测量或给出准确率。'],
            'engineering_verified':False}


def inline_annotations(texts,axes,members,bom,excluded):
    results=[];used_lengths=set()
    def section(spec):
        match=re.search(r'L\d+(?:\.\d+)?X\d+(?:X\d+(?:\.\d+)?)?',spec)
        return match.group() if match else spec
    for text in texts:
        compact=text['text'].upper().replace('×','X').replace('*','X')
        if not re.fullmatch(r'(?:Q\d+[A-Z]*)?L\d+(?:\.\d+)?X\d+(?:X\d+(?:\.\d+)?)?',compact):continue
        lengths=[t for t in texts if t['id'] not in excluded and t['text_block']==text['text_block'] and t['text_line']>text['text_line']
                 and re.fullmatch(r'\d+(?:\.\d+)?',t['text'])]
        if not lengths:
            u=np.array(text['direction']);normal=np.array([-u[1],u[0]]);c=midpoint(text['bbox_pt']);neighbors=[]
            for t in texts:
                if t['id'] in excluded|used_lengths or not re.fullmatch(r'\d+(?:\.\d+)?',t['text']):continue
                delta=midpoint(t['bbox_pt'])-c;along=abs(float(delta@u));below=float(delta@normal)
                if abs(float(u@np.array(t['direction'])))<.995 or not 1<below<max(12,text['font_size_pt']*1.9):continue
                if along<max(7,text['font_size_pt']*1.5):neighbors.append((along+below,t))
            neighbors.sort(key=lambda p:p[0])
            if neighbors and (len(neighbors)==1 or neighbors[1][0]-neighbors[0][0]>2):lengths=[neighbors[0][1]]
        if len(lengths)!=1:continue
        used_lengths.add(lengths[0]['id'])
        label=lengths[0];c=midpoint(text['bbox_pt']);options=[];seen=set()
        for m in members:
            if m['callout_id'] in seen:continue
            seen.add(m['callout_id']);box=m['callout_bbox_pt'];distance=float(np.linalg.norm(c-midpoint(box)))-(box[2]-box[0])/2
            if distance<75:options.append((max(0,distance),m['callout_id']))
        if not options:continue
        options.sort(key=lambda p:p[0]);cid=options[0][1];ambiguous=len(options)>1 and options[1][0]-options[0][0]<5
        for m in members:
            if m['callout_id']!=cid:continue
            mid=m['id'];g=next((g for g in axes if mid in g['member_ids']),None)
            length=float(label['text']);spec=section(compact)
            bom_spec=section(m['bom']['classification']['normalized'])
            matches=[r['mark'] for r in bom if section(r['classification']['normalized'])==spec and r['length_mm']==length]
            results.append({'id':f"{m['view_id']}_annotation_{len(results)+1:03d}",'view_id':m['view_id'],'member_id':mid,'mark':m['mark'],
                            'specification':compact,'length_mm':length,'bom_specification':m['bom']['specification'],'bom_length_mm':m['bom']['length_mm'],
                            'source_text_ids':[text['id'],label['id']],'axis_id':g['id'] if g else None,'association_status':'ambiguous' if ambiguous else 'nearby_callout_candidate',
                            'outline_direction_agrees':bool(g and abs(float(np.array(text['direction'])@np.array(g['u'])))>math.cos(math.radians(8))),
                            'status':'consistent' if length==m['bom']['length_mm'] and spec==bom_spec else 'bom_annotation_conflict',
                            'matching_bom_marks':matches,'comparison_scope':'angle_section_dimensions_and_length_only_not_steel_grade'})
    return results


def audit_dimensions(run_dir):
    run_dir=Path(run_dir);site=run_dir/'site';top=site/'topology';dest=site/'audit';dest.mkdir(parents=True,exist_ok=True)
    manifest=read_json(run_dir/'manifest.json');source=Path(manifest['source'])
    if file_digest(source)!=manifest['source_sha256']:raise ValueError('Source PDF changed; regenerate decomposition and topology')
    from .ocr_input import analysis_source
    source = analysis_source(manifest)
    report=read_json(top/'report.json');views=read_json(top/'views.json');members=read_json(top/'members.json')
    if report['source_sha256']!=manifest['source_sha256'] or any(file_digest(site/name)!=value for name,value in report['input_files'].items()):
        raise ValueError('Topology inputs changed; regenerate topology before auditing')
    primitives=read_json(site/'structure/primitives.json');bom=read_json(site/'tables/bom.json');callouts=read_json(site/'structure/callouts.json')
    texts=text_geometry(primitives,source);excluded={m['text_id'] for c in callouts for m in c['marks']}
    all_polygons=discover_polygons(primitives,texts,views,members,excluded)
    annotations=[];view_by_id={v['id']:v for v in views};texts_by_view={}
    for vid in {p['view_id'] for p in all_polygons}-set(view_by_id):
        placeholder={'id':vid,'title':'未归属结构面的尺寸图','page':next(p['page'] for p in all_polygons if p['view_id']==vid),'primitive_ids':[],'bbox_pt':[0,0,0,0]}
        views.append(placeholder);view_by_id[vid]=placeholder
    for view in views:
        vt=[texts[i] for i in view['primitive_ids'] if i in texts];vm=[m for m in members if m['view_id']==view['id']]
        texts_by_view[view['id']]=vt;axes=projected_axes(vm)
        polygons=[p for p in all_polygons if p['view_id']==view['id']]
        for poly in polygons:
            associate_polygon(poly,axes,view)
            complete=all('value_mm' in e for e in poly['edges']);poly['labels_complete']=complete
            if complete:
                values=[e['value_mm'] for e in poly['edges']]
                poly['arithmetic']=cosine_triangle(values) if len(values)==3 else {'status':'polygon_inequality_passed' if max(values)<sum(values)-max(values) else 'polygon_inequality_failed','shape_unique_from_sides':False}
                if poly['association_status']=='projected_face_candidate':
                    poly['closure']=vector_check(values,[e['direction'] for e in poly['edges']],[e['rounding_mm'] for e in poly['edges']])
        annotations.extend(inline_annotations(vt,axes,vm,bom,excluded|{e['text_id'] for p in polygons for e in p['edges'] if 'text_id' in e}))
    spans=span_bom_checks(all_polygons,texts_by_view,members)
    straight_checks=[]
    for a,b in itertools.permutations(all_polygons,2):
        if a['view_id']==b['view_id'] and a['association_status']==b['association_status']=='projected_face_candidate':straight_checks.extend(straight_triangle_check(a,b))
    observed={m['mark'] for m in members};findings=[]
    for poly in all_polygons:
        if poly.get('arithmetic',{}).get('status') in {'triangle_inequality_failed','polygon_inequality_failed'}:
            findings.append({'id':f"finding_{len(findings)+1:03d}",'kind':'impossible_side_lengths','view_id':poly['view_id'],'polygon_id':poly['id'],
                             'summary':'边长不满足构成闭合多边形的必要条件，需核对标注读取和边的对应。','confidence':'unresolved_source_or_association_conflict'})
        check=poly.get('closure')
        if not check or check['status']!='directional_inconsistency':continue
        decision=compare_dimension_hypotheses(poly,spans,straight_checks);preferred=decision['preferred']
        edge=next((e for e in poly['edges'] if preferred and e['text_id']==preferred['changed_text_id']),None)
        support=[c for c in straight_checks if edge and c['target_text_id']==edge['text_id']]
        findings.append({'id':f"finding_{len(findings)+1:03d}",'kind':'dimension_direction_conflict','view_id':poly['view_id'],'polygon_id':poly['id'],
                         'summary':'尺寸闭合存在矛盾；该单值候选通过当前可用的交叉检查，建议优先核对。' if edge else '尺寸闭合存在矛盾，当前没有通过全部可用检查的唯一单值候选。',
                         'observed_mm':edge['value_mm'] if edge else None,'recommended_mm':preferred['candidate_mm'] if preferred else None,
                         'source_text_id':edge['text_id'] if edge else None,'calculation':check,'independent_triangle_checks':support,
                         'decision':decision,'confidence':decision['status']})
    for record in annotations:
        if record['status']!='bom_annotation_conflict':continue
        unobserved=[mark for mark in record['matching_bom_marks'] if mark not in observed]
        record['preferred_unobserved_mark']=unobserved[0] if len(unobserved)==1 and record['association_status']!='ambiguous' and record['outline_direction_agrees'] else None
        candidate_records=[]
        for mark in record['matching_bom_marks']:
            row=next(b for b in bom if b['mark']==mark)
            candidate_records.append({'mark':mark,'bom_quantity':row['quantity'],
                                      'same_view_other_member_ids':[m['id'] for m in members if m['mark']==mark and m['view_id']==record['view_id'] and m['id']!=record['member_id']],
                                      'observed_anywhere':mark in observed})
        record['mark_candidates']=candidate_records
        record['current_mark_consistent_observations']=[{'member_id':a['member_id'],'view_id':a['view_id'],'specification':a['specification'],'length_mm':a['length_mm']}
            for a in annotations if a['member_id']!=record['member_id'] and a['mark']==record['mark'] and a['status']=='consistent'
            and a['association_status']!='ambiguous' and a['outline_direction_agrees']]
        findings.append({'id':f"finding_{len(findings)+1:03d}",'kind':'bom_annotation_conflict','view_id':record['view_id'],'member_id':record['member_id'],
                         'summary':f"圈号 {record['mark']} 对应 BOM 与杆旁规格/下料长度不一致。",'annotation':record,
                         'confidence':'mark_correction_candidate' if record['preferred_unobserved_mark'] else 'unresolved_source_conflict'})
    for check in spans:
        if check['status']=='end_basis_or_length_conflict':
            findings.append({'id':f"finding_{len(findings)+1:03d}",'kind':'span_bom_conflict','view_id':check['view_id'],'span_check_id':check['id'],
                             'summary':'跨度与两端正负尺寸换算后不符 BOM，须先核实是否为整杆跨度以及两端尺寸的基准。','confidence':'conditional_candidate','calculation':check})
    doc=fitz.open(source)
    for poly in all_polygons:
        b=poly['bbox_pt'];boxes=[b]+[e['label_bbox_pt'] for e in poly['edges'] if 'label_bbox_pt' in e]
        clip=fitz.Rect(min(b[0] for b in boxes)-5,min(b[1] for b in boxes)-5,max(b[2] for b in boxes)+5,max(b[3] for b in boxes)+5)
        name=poly['id']+'.png';doc[poly['page']-1].get_pixmap(matrix=fitz.Matrix(4,4),clip=clip).save(dest/name);poly['image']=name
    for check in spans:
        if not check['end_basis']['selected']:continue
        poly=next(p for p in all_polygons if p['id']==check['polygon_id']);edge=next(e for e in poly['edges'] if e.get('text_id')==check['span_text_id'])
        points=[edge['structural_a_pt'],edge['structural_b_pt']]+[pt for b in [c['bbox_pt'] for c in check['end_basis']['selected']] for pt in (b[:2],b[2:])]
        coords=np.array(points);clip=fitz.Rect(*(coords.min(axis=0)-18),*(coords.max(axis=0)+18))
        name=check['id']+'.png';doc[poly['page']-1].get_pixmap(matrix=fitz.Matrix(3,3),clip=clip).save(dest/name);check['image']=name
    for finding in findings:
        if finding['kind']!='bom_annotation_conflict':continue
        record=finding['annotation'];member=next(m for m in members if m['id']==record['member_id'])
        boxes=[member['callout_bbox_pt']]+[texts[tid]['bbox_pt'] for tid in record['source_text_ids']]
        clip=fitz.Rect(min(b[0] for b in boxes)-18,min(b[1] for b in boxes)-18,max(b[2] for b in boxes)+18,max(b[3] for b in boxes)+18)
        name=finding['id']+'.png';doc[view_by_id[finding['view_id']]['page']-1].get_pixmap(matrix=fitz.Matrix(4,4),clip=clip).save(dest/name);finding['image']=name
    doc.close()
    view_summaries=summarize_views(views,all_polygons,spans,annotations,findings)
    result={'schema_version':'1.1','stage':'preliminary_dimension_audit','status':'needs_review','source_sha256':manifest['source_sha256'],
            'input_sha256':digest({str(p.relative_to(site)):file_digest(p) for p in [top/'report.json',top/'views.json',top/'members.json',top/'geometry.json',site/'tables/bom.json',site/'structure/primitives.json',site/'structure/callouts.json']}),
            'title':report['title'],'views':[{'id':v['id'],'title':v['title']} for v in views],'polygons':all_polygons,'annotations':annotations,'span_bom_checks':spans,
            'straight_triangle_checks':straight_checks,'findings':findings,'view_summaries':view_summaries,'engineering_accuracy_verified':False,
            'counts':{'dimension_polygons':len(all_polygons),'complete_labels':sum(p['labels_complete'] for p in all_polygons),
                      'direction_checked':sum('closure' in p for p in all_polygons),'inline_bom_checks':len(annotations),'findings':len(findings),
                      'correspondence_pending':sum(p['association_status']!='projected_face_candidate' for p in all_polygons),
                      'native_support_resolved':sum(p.get('native_support_resolved_ambiguity',False) for p in all_polygons),
                      'span_bom_checked':sum('computed_cut_length_mm' in c for c in spans),'span_basis_pending':sum(c['status']=='basis_incomplete' for c in spans),
                      'annotation_association_pending':sum(a['association_status']=='ambiguous' for a in annotations),
                      'member_direction_disagreements':sum(not a['outline_direction_agrees'] for a in annotations)},
            'limitations':['示意图尺寸通常是节点跨度；BOM 是下料长度，必须有端部退距、伸长或孔中心基准才能直接换算。',
                           '未假设小三角形为直角；普通三角形使用余弦定理。四边形及以上仅凭边长通常不能唯一确定形状。',
                           '方向闭合依赖结构面的投影与杆件对应正确，采用原始轮廓方向，未用 BOM 补长后的长度自证。',
                           '候选结构边须与原始可见杆段或原生实线延续有实际重叠；只有远处延长线支持时保留待定。此位置检查不确认下料端点。',
                           '推荐值用于初审排查，不自动修改图纸、BOM、杆件件号或三维模型；未关联示意图保留待定。',
                           '当前自动检测覆盖原生矢量中独立闭合、带数字的 3～8 边示意图；断线、连线混杂及扫描图需人工复核，检测数不代表全图已核完。']}
    write_json(dest/'report.json',result)
    for asset in (Path(__file__).parent/'audit_web').iterdir():
        if asset.is_file():shutil.copy2(asset,dest/asset.name)
    topology_index=top/'index.html'
    html=topology_index.read_text(encoding='utf-8')
    if 'id="audit-link"' not in html:
        topology_index.write_text(html.replace('href="graph.json" download','id="audit-link" href="../audit/">尺寸初审</a> · <a href="graph.json" download',1),encoding='utf-8')
    lines=['# 图纸尺寸初审','',f"识别尺寸示意图 {len(all_polygons)} 个，见本地页面 /audit/。",'',*['- '+s for s in result['limitations']],'']
    lines.extend(['## 逐面审核进度','','计数为尺寸图与候选观察记录，不是实物数量；没有冲突记录不表示全图通过。','',
                  '| 结构面 | 尺寸图 | 闭合吻合 / 冲突 | 对应待定 | BOM 换算吻合 / 已算 | 换算依据不足 | 初审结论 |',
                  '| --- | ---: | --- | ---: | --- | ---: | --- |'])
    for summary in view_summaries:
        c=summary['counts'];lines.append(f"| {summary['title']} | {c['dimension_polygons']} | {c['direction_consistent']} / {c['direction_conflicts']} | {c['correspondence_pending']} | {c['span_bom_consistent']} / {c['span_bom_checked']} | {c['span_basis_pending']} | {summary['conclusion']} |")
    lines.append('')
    for f in findings:
        lines.extend([f"## {view_by_id[f['view_id']]['title']} / {f['id']}",'',f['summary'],''])
        if f['kind']=='dimension_direction_conflict':
            lines.extend([f"原值：{f['observed_mm']} mm；推荐候选：{f['recommended_mm']} mm。" if f['recommended_mm'] is not None else '当前证据无法选出唯一修正值。',
                          f"闭合残差：{f['calculation']['residual_norm_mm']:.3f} mm；取整误差界：{f['calculation']['rounding_bound_mm']:.3f} mm。",'',
                          '| 假设 | 方向闭合残差 | 相邻尺寸反算 | BOM 换算 | 综合判断 |','| --- | ---: | --- | --- | --- |'])
            for h in f['decision']['hypotheses']:
                adjacent='；'.join('无唯一反算解' if t['passed'] is None else f"目标 {t['target_mm']:g}，反算 {t['computed_mm']:.3f} mm，{'满足' if t['passed'] else '不满足'}取整区间" for t in h['triangle_checks']) or '暂无可用依据'
                converted='；'.join(f"件号 {b['mark']}：{b['computed_cut_length_mm']:g} / BOM {b['bom_length_mm']:g} mm，差 {b['delta_mm']:g}" for b in h['bom_checks']) or '暂无完整换算依据'
                conclusion='通过可用检查' if h['all_available_checks_passed'] else '未通过全部检查'
                lines.append(f"| {h['label']} | {h['residual_mm']:.3f} mm | {adjacent} | {converted} | {conclusion} |")
            lines.extend(['',f['decision']['selection_rule'],''])
        elif f['kind']=='bom_annotation_conflict':
            a=f['annotation'];lines.extend([f"杆旁：{a['specification']} / {a['length_mm']} mm；BOM：{a['bom_specification']} / {a['bom_length_mm']} mm。",f"匹配 BOM 件号：{a['matching_bom_marks']}；尚未观察到的优先候选：{a['preferred_unobserved_mark']}。",''])
            for evidence in a['current_mark_consistent_observations']:
                lines.extend([f"圈号 {a['mark']} 在 {view_by_id[evidence['view_id']]['title']} 的另一处观察：{evidence['specification']} / {evidence['length_mm']:g} mm 与 BOM 匹配，支持优先核对本处圈号。",''])
            for candidate in a['mark_candidates']:
                lines.append(f"- 候选件号 {candidate['mark']}：BOM 数量 {candidate['bom_quantity']}，当前面其他位置观察数 {len(candidate['same_view_other_member_ids'])}（观察数不等于实物数量）。")
            lines.append('')
    lines.extend(['## 跨度与 BOM 换算','','只有明确读取两端正负标注的记录才计算；端部标注与整杆基准仍需确认。',''])
    for check in spans:
        if 'formula' in check:lines.extend([f"- {view_by_id[check['view_id']]['title']} / 件号 {check['mark']}：{check['formula']}；BOM {check['bom_length_mm']:g} mm；{'在当前基准下吻合' if check['status']=='consistent_given_end_basis' else '端部基准或长度需要复核'}。"])
    lines.extend(['',f"尚有 {result['counts']['span_basis_pending']} 条尺寸边与杆件对应记录缺少完整端部换算依据。",''])
    lines.extend(['| 结构面 / 件号 | 尺寸边 mm | BOM mm | 缺失条件 |','| --- | ---: | ---: | --- |'])
    for check in spans:
        if check['status']=='basis_incomplete':
            reasons='；'.join(r['message'] for r in check['pending_reasons'])
            lines.append(f"| {view_by_id[check['view_id']]['title']} / {check['mark']} | {check['span_mm']:g} | {check['bom_length_mm']:g} | {reasons} |")
    (run_dir/'dimension-audit.md').write_text('\n'.join(lines),encoding='utf-8')
    failure=run_dir/'failure.json'
    if failure.exists() and read_json(failure).get('stage')=='audit':failure.unlink()
    print(f"[audit] {result['counts']}; status=needs_review",flush=True)
    return result
