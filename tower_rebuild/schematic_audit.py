"""Overall schematic dimensions are master constraints, never pixel-derived truth."""
import copy
import html
import math
import re
import shutil
from collections import Counter
from pathlib import Path

import numpy as np

from .io import digest, file_digest, read_json, write_json
from .topology_geometry import unit_segment, point_segment_distance


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('尺寸必须是有限数值。')
    return float(value)


def discover_dimensions(views, geometry, texts, excluded):
    """Dimension lines need two perpendicular witness lines; circles are excluded.

    Their endpoints locate a measurement interval, not a physical member. A
    horizontal/vertical overall dimension must not be mistaken for slope length.
    """
    result = []
    for view in views:
        if view.get('role') != 'front_schematic':
            continue
        strokes = next((g['strokes'] for g in geometry if g['view_id'] == view['id']), [])
        for tid in view['primitive_ids']:
            t = texts.get(tid)
            if not t or tid in excluded or not re.fullmatch(r'\d+(?:\.\d+)?', t['text']) or float(t['text']) <= 0:
                continue
            center = np.array([(t['bbox_pt'][0]+t['bbox_pt'][2])/2, (t['bbox_pt'][1]+t['bbox_pt'][3])/2])
            options = []
            for line in strokes:
                info = unit_segment(line['a'], line['b'])
                if not info: continue
                a,b,u,length = info
                if abs(u@np.asarray(t['direction'])) < .995: continue
                distance, along = point_segment_distance(center,a,b)
                if not .05 <= along <= .95 or distance > t['font_size_pt']*1.7: continue
                witnesses = []
                for other in strokes:
                    oi=unit_segment(other['a'],other['b'])
                    if not oi or oi[3] < 5 or abs(u@oi[2]) > .08: continue
                    matrix=np.column_stack((b-a,oi[0]-oi[1]))
                    x,y=np.linalg.solve(matrix,oi[0]-a)
                    if -.02 <= x <= 1.02 and -.08 <= y <= 1.08:
                        witnesses.append((float(x), (a+x*(b-a)).tolist(), other))
                left=[w for w in witnesses if abs(w[0]*length) < 5]
                right=[w for w in witnesses if abs((1-w[0])*length) < 5]
                if not left or not right: continue
                l=min(left,key=lambda w:abs(w[0]));r=min(right,key=lambda w:abs(1-w[0]))
                if math.dist(l[1],r[1]) < 4: continue
                options.append((distance, line, l, r, u))
            options.sort(key=lambda o:o[0])
            selected = options[0] if options and (len(options)==1 or options[1][0]-options[0][0]>.5) else None
            row={'id':'schematic_'+tid,'view_id':view['id'],'page':view['page'],'text_id':tid,
                 'value_mm':float(t['text']),'label_bbox_pt':t['bbox_pt'],
                 'status':'dimension_interval_candidate' if selected else 'dimension_endpoints_pending',
                 'measurement':'pending','a_pt':None,'b_pt':None,'evidence_ids':[tid]}
            if selected:
                _,line,l,r,u=selected
                row.update(a_pt=l[1],b_pt=r[1],measurement='x' if abs(u[0])>.995 else 'y' if abs(u[1])>.995 else 'axial',
                           evidence_ids=[tid]+line['primitive_ids']+l[2]['primitive_ids']+r[2]['primitive_ids'])
            result.append(row)
    return result


def factor(axis, measurement):
    d=np.asarray(axis['b'],float)-axis['a'];length=float(np.linalg.norm(d))
    if length<1e-8: raise ValueError('杆件轴线长度为零。')
    return 1. if measurement=='axial' else abs(float(d[0 if measurement=='x' else 1]))/length


def prepare_schematic(run, views, geometry, texts, excluded, members, bom, mapping_path=None):
    run=Path(run);site=run/'site';manifest=read_json(run/'manifest.json')
    labels=discover_dimensions(views,geometry,texts,excluded)
    dimensions=[d for d in labels if d['status']=='dimension_interval_candidate']
    signature=digest({'dimensions':dimensions,'members':members,'bom':bom,'geometry':geometry})
    default=site/'schematic/mapping.json'
    mapping=read_json(mapping_path or default) if (mapping_path or default.exists()) else {
        'source_sha256':manifest['source_sha256'],'input_sha256':signature,'bindings':[]}
    if mapping.get('source_sha256')!=manifest['source_sha256'] or mapping.get('input_sha256')!=signature:
        raise ValueError('单线图对应记录与原图/当前识别结果不一致，请重新生成对应记录。')
    lookup={m['id']:m for m in members};bs={b['mark']:b for b in bom};ds={d['id']:d for d in dimensions}
    base_dimensions=dict(ds)
    for derived in mapping.get('derived_dimensions',[]):
        did=derived.get('id');terms=derived.get('terms',[]);mode=derived.get('measurement')
        if not isinstance(did,str) or not did.startswith('derived_') or did in ds or not terms or mode not in ('x','y','axial'):
            raise ValueError('组合尺寸需要唯一 derived_ ID、方向和原标注计算项。')
        total=0.;evidence=[];formula=[];seen=set()
        for term in terms:
            tid=term.get('dimension_id');coefficient=number(term.get('factor'))
            if tid not in base_dimensions or tid in seen or coefficient==0 or abs(coefficient)>10:
                raise ValueError('组合尺寸须引用不重复的原单线图尺寸，系数范围为 -10 至 10（不含 0）。')
            original=base_dimensions[tid];seen.add(tid)
            if original['measurement']!=mode:raise ValueError('组合尺寸的各项投影方向必须一致。')
            total+=original['value_mm']*coefficient;evidence+=original['evidence_ids']
            formula.append(f"{original['value_mm']:g} × {coefficient:g}")
        if total<=0:raise ValueError('组合整体尺寸必须大于零。')
        ds[did]={'id':did,'view_id':base_dimensions[terms[0]['dimension_id']]['view_id'],
                 'value_mm':total,'measurement':mode,'status':'dimension_interval_candidate',
                 'terms':terms,'formula':' + '.join(formula)+f' = {total:g} mm',
                 'scope':derived.get('scope',''),'basis':'user_confirmed_dimension_chain','evidence_ids':sorted(set(evidence))}
        dimensions.append(ds[did])
    bindings=mapping.get('bindings')
    if not isinstance(bindings,list):raise ValueError('单线图 bindings 必须是数组。')
    reference_only=mapping.get('reference_only_dimensions',[])
    if not isinstance(reference_only,list) or any(i not in base_dimensions for i in reference_only):
        raise ValueError('仅作参考的尺寸必须来自当前单线图。')
    formula_inputs={t['dimension_id'] for d in mapping.get('derived_dimensions',[]) for t in d['terms']}
    used=set();rows=[]
    for binding in bindings:
        did=binding['dimension_id']
        if did not in ds or did in used or did in reference_only:raise ValueError('单线图尺寸不存在、被重复绑定或已设为仅作参考。')
        used.add(did);dim=ds[did];ids=binding.get('member_ids',[])
        if not ids or len(ids)!=len(set(ids)) or any(i not in lookup for i in ids):raise ValueError('请指定存在且不重复的杆件观察 ID。')
        ms=[lookup[i] for i in ids]
        if len({m['view_id'] for m in ms})!=1 or len({m.get('side') for m in ms}-{None})>1:
            raise ValueError('同一尺寸链须来自同一结构面/同一侧，不能把前后件重复相加。')
        mode=binding.get('measurement',dim['measurement'])
        if mode not in ('x','y','axial') or dim['measurement'] not in (mode,'pending'):
            raise ValueError('尺寸链投影方向与单线图尺寸不符。')
        offsets=[]
        for term in binding.get('offsets',[]):
            kind=term.get('kind');value=number(term.get('value_mm'))
            if kind not in ('end_extension','overlap','setback') or value<0:raise ValueError('端部项须为非负长度，类型为伸出、搭接或退距。')
            if term.get('confirmed') is not True:raise ValueError('端套/搭接贡献长度须先确认。')
            source=None
            if term.get('text_id') in texts:
                text=texts[term['text_id']]
                if re.fullmatch(r'[+-]?\d+(?:\.\d+)?',text['text']) and abs(abs(float(text['text']))-value)<1e-6:
                    source={'text_id':text['id'],'bbox_pt':text['bbox_pt'],'page':text['page']}
            if term.get('bom_mark') in bs and abs(bs[term['bom_mark']]['length_mm']-value)<1e-6:
                source={'bom_mark':term['bom_mark'],'basis':'confirmed_full_stock_contribution'}
            if source is None:raise ValueError('端部长度必须对应原图数字或 BOM 长度；不接受无来源补差值。')
            offsets.append({**term,'signed_mm':value if kind=='end_extension' else -value,'evidence':source})
        coefficients=[factor(m['axis'],mode) for m in ms]
        total=sum(m['bom']['length_mm']*f for m,f in zip(ms,coefficients))+sum(t['signed_mm'] for t in offsets)
        confirmed=(binding.get('correspondence_confirmed') is True and binding.get('end_basis_confirmed') is True
                   and dim['status']=='dimension_interval_candidate')
        correction=binding.get('correction')
        if correction and (correction.get('member_id') not in ids or correction.get('anchor') not in ('a','b')):
            raise ValueError('修正必须指定本尺寸链中的一根杆件及固定端 a/b。')
        rows.append({'dimension_id':did,'dimension':dim,'view_id':ms[0]['view_id'],'member_ids':ids,
                     'marks':[m['mark'] for m in ms],'measurement':mode,'coefficients':coefficients,
                     'offsets':offsets,'bom_chain_mm':total,'bom_delta_mm':total-dim['value_mm'],
                     'status':'pending' if not confirmed else 'difference' if abs(total-dim['value_mm'])>3 else 'consistent_given_end_basis',
                     'member_correspondence_confirmed':binding.get('correspondence_confirmed') is True,
                     'correspondence_confirmed':confirmed,'correction_request':correction,
                     'topology_connectivity_verified':False,'engineering_accuracy_verified':False})
        rows[-1]['shared_outline_conflicts']=[{'member_id':m['id'],'mark':m['mark'],
            'other_member_id':other['id'],'other_mark':other['mark'],'other_bom_length_mm':other['bom']['length_mm']}
            for m in ms for other in members if other['id']!=m['id'] and other['view_id']==m['view_id']
            and m.get('band_id') and m.get('band_id')==other.get('band_id')
            and other.get('side') in (None,m.get('side')) and other['bom']['length_mm']!=m['bom']['length_mm']]
    for d in dimensions:
        if d['id'] not in used:rows.append({'dimension_id':d['id'],'dimension':d,'view_id':d['view_id'],
             'member_ids':[],'marks':[],'status':'reference_only' if d['id'] in set(reference_only)|formula_inputs else 'pending',
             'reason':'塔身/组合尺寸参考，不独立绑定横担杆件。' if d['id'] in set(reference_only)|formula_inputs else '先确认结构面、件号及端套伸出/搭接基准。'})
    report={'schema_version':'1.0','source_sha256':manifest['source_sha256'],'input_sha256':signature,
            'reference_basis':'schematic_annotation_is_overall_master','dimensions':dimensions,'rows':rows,
            'unresolved_numeric_labels':[d for d in labels if d['status']!='dimension_interval_candidate'],
            'counts':dict(Counter(r['status'] for r in rows)),'engineering_accuracy_verified':False}
    write_json(default,mapping)
    write_json(site/'schematic/template.json',{'source_sha256':manifest['source_sha256'],'input_sha256':signature,
         'bindings':[], 'instructions':'将确认的尺寸 ID、杆件观察 ID 和有原图依据的端部项填入 bindings，参考 README。',
         'dimensions':dimensions,'members':[{'id':m['id'],'mark':m['mark'],'view_id':m['view_id'],'bom_length_mm':m['bom']['length_mm']} for m in members]})
    write_json(site/'schematic/report.json',report)
    import fitz
    from .ocr_input import analysis_source
    with fitz.open(analysis_source(manifest)) as doc:
        for view in views:
            if view.get('role')=='front_schematic':
                doc[view['page']-1].get_pixmap(matrix=fitz.Matrix(2,2),clip=fitz.Rect(view['bbox_pt'])).save(site/'schematic'/f"{view['id']}.png")
    publish_schematic(site,report)
    return report


def native_support(axis, proposed, strokes, half_width_pt):
    """Require both parallel native outlines over added span, or a native cut cap."""
    a,b,u,length=unit_segment(axis['a'],axis['b']);n=np.array([-u[1],u[0]])
    old=sorted([float((np.array(axis[k])-a)@u) for k in ('a','b')])
    new=sorted([float((np.array(proposed[k])-a)@u) for k in ('a','b')])
    added=[(new[0],old[0])] if new[0]<old[0]-.1 else []
    if new[1]>old[1]+.1:added.append((old[1],new[1]))
    evidence=[];tracks={-1:[],1:[]};caps=[]
    for s in strokes:
        info=unit_segment(s['a'],s['b'])
        if not info:continue
        c,d,v,_=info;offset=float(((c+d)/2-a)@n)
        if abs(v@u)>.999 and abs(abs(offset)-half_width_pt)<max(.25,half_width_pt*.08):
            tracks[1 if offset>0 else -1].append((min((c-a)@u,(d-a)@u),max((c-a)@u,(d-a)@u),s['primitive_ids']))
        elif abs(v@u)<.1:
            for endpoint in proposed.values():
                if point_segment_distance(endpoint,c,d)[0]<.5:caps+=s['primitive_ids']
    for lo,hi in added:
        for values in tracks.values():
            end=lo
            for x,y,ids in sorted(values):
                if y<end-.5:continue
                if x>end+.5:break
                end=max(end,y);evidence+=ids
            if end<hi-.5:return {'supported':False,'primitive_ids':[],'reason':'补段没有连续双侧原生轮廓支持。'}
    if not added and not caps:return {'supported':False,'primitive_ids':[],'reason':'截短位置没有原生端面线支持。'}
    return {'supported':True,'primitive_ids':sorted(set(evidence+caps)),'reason':'原生双侧补段或端面线支持；仍为候选。'}


def apply_schematic_corrections(model, schematic, geometry):
    lookup={m['id']:(v,m) for v in model['views'] for m in v['members']}
    proposals=[];targets=Counter((r.get('correction_request') or {}).get('member_id') for r in schematic['rows'])
    for row in schematic['rows']:
        request=row.get('correction_request')
        if not request:continue
        proposal={'dimension_id':row['dimension_id'],'member_id':request['member_id'],'status':'blocked'}
        proposals.append(proposal)
        mid=request['member_id'];ids=row['member_ids']
        if targets[mid]>1 or row.get('shared_outline_conflicts') or row['status']!='consistent_given_end_basis' or any(i not in lookup for i in ids):
            proposal['reason']='对应/端部/BOM 与单线图冲突、缺模型或同杆有多个修正约束。';continue
        view,member=lookup[mid];ratio=view['scale']['mm_per_pt'];index=ids.index(mid);coefficient=row['coefficients'][index]
        if not ratio or coefficient<.01 or any(not lookup[i][0]['scale']['mm_per_pt'] for i in ids):
            proposal['reason']='缺少比例或该杆件在尺寸方向没有足够投影。';continue
        # Other rods use their actual model lengths. Fixing this rod may not
        # absorb errors in another rod or in the terminal contribution.
        others=sum(math.dist(lookup[i][1]['axis_pt']['a'],lookup[i][1]['axis_pt']['b'])*lookup[i][0]['scale']['mm_per_pt']*f
                   for i,f in zip(ids,row['coefficients']) if i!=mid)
        target=(row['dimension']['value_mm']-sum(t['signed_mm'] for t in row['offsets'])-others)/coefficient
        if target<=0 or abs(target-member['bom_length_mm'])>3:
            proposal['reason']='反算杆长与 BOM 不符，不能把其他尺寸误差转移到此杆。';continue
        old=copy.deepcopy(member['axis_pt']);anchor=request['anchor'];moving='b' if anchor=='a' else 'a'
        d=np.array(old[moving])-old[anchor];d=d/np.linalg.norm(d)
        proposed=copy.deepcopy(old);proposed[moving]=(np.array(old[anchor])+d*target/ratio).tolist()
        strokes=next(g['strokes'] for g in geometry if g['view_id']==view['view_id'])
        support=native_support(old,proposed,strokes,member['section']['legs_mm'][0]/ratio/2)
        proposal.update(original_axis_pt=old,axis_pt=proposed,target_length_mm=target,
                        original_length_mm=math.dist(old['a'],old['b'])*ratio,native_evidence=support)
        if not support['supported']:proposal['reason']=support['reason'];continue
        proposal.update(status='ready_candidate',reason='单线图、端部尺寸链、BOM 与原生线条共同支持。')
        if request.get('apply') is True:
            proposal['status']='applied_candidate'
            member.update(axis_pt=proposed,projected_preview_length_mm=target,delta_to_bom_mm=target-member['bom_length_mm'],
                          axis_source='schematic_master_with_bom_and_native_evidence',schematic_correction=copy.deepcopy(proposal))
    model['schematic_corrections']=proposals
    return proposals


def diagnose_structure(model, schematic, geometry):
    lookup={m['id']:(v,m) for v in model['views'] for m in v['members']}
    findings=[]
    for row in schematic['rows']:
        if not row.get('member_correspondence_confirmed',row.get('correspondence_confirmed')):continue
        for mid in row['member_ids']:
            if mid not in lookup:continue
            view,m=lookup[mid];ratio=view['scale']['mm_per_pt']
            if not ratio:continue
            axis=m.get('schematic_correction',{}).get('original_axis_pt',m['axis_pt'])
            length=math.dist(axis['a'],axis['b'])*ratio;missing=m['bom_length_mm']-length
            if abs(missing)<=3:continue
            candidates=[]
            for anchor in ('a','b'):
                moving='b' if anchor=='a' else 'a';d=np.asarray(axis[moving])-axis[anchor];d=d/np.linalg.norm(d)
                proposed=copy.deepcopy(axis);proposed[moving]=(np.asarray(axis[anchor])+d*m['bom_length_mm']/ratio).tolist()
                support=native_support(axis,proposed,next(g['strokes'] for g in geometry if g['view_id']==view['view_id']),m['section']['legs_mm'][0]/ratio/2)
                candidates.append({'anchor':anchor,'axis_pt':proposed,**support})
            findings.append({'dimension_id':row['dimension_id'],'member_id':mid,'mark':m['mark'],
                             'original_model_length_mm':length,'bom_length_mm':m['bom_length_mm'],
                             'missing_or_excess_mm':missing,'endpoint_candidates':candidates,
                             'diagnosis':'native_supported_extent_candidate' if any(c['supported'] for c in candidates) else 'no_native_support_check_mark_projection_and_end_basis'})
    return findings


def model_checks(model, schematic, tolerance=3):
    lookup={m['id']:(v,m) for v in model['views'] for m in v['members']};rows=[]
    for r in schematic['rows']:
        row=copy.deepcopy(r);row.update(kind='schematic_overall',reference_mm=r['dimension']['value_mm'],measured_mm=None,delta_mm=None,status='pending')
        if r['status']=='reference_only':row['status']='reference_only'
        if r.get('member_correspondence_confirmed',r.get('correspondence_confirmed')) and all(i in lookup for i in r['member_ids']):
            measured=0.;original=0.;complete=True
            for mid in r['member_ids']:
                v,m=lookup[mid];ratio=v['scale']['mm_per_pt']
                if not ratio:complete=False;break
                axis=m['axis_pt'];old=m.get('schematic_correction',{}).get('original_axis_pt',axis)
                measured+=math.dist(axis['a'],axis['b'])*ratio*factor(axis,r['measurement'])
                original+=math.dist(old['a'],old['b'])*ratio*factor(old,r['measurement'])
            if complete:
                offset=sum(t['signed_mm'] for t in r['offsets']);measured+=offset;original+=offset
                row.update(measured_mm=measured,original_measured_mm=original,delta_mm=measured-row['reference_mm'],
                    status='pending' if not r.get('correspondence_confirmed') else 'difference' if abs(measured-row['reference_mm'])>tolerance else 'within_diagnostic_bound',
                    reference_reused_for_correction=any('schematic_correction' in lookup[i][1] for i in r['member_ids']))
        rows.append(row)
    return rows


def assembly_checks(source, faces):
    """Re-measure the final local 3D axes, including assembly-layer repairs.

    Rigid placement preserves these lengths/projections in the face's reference
    frame. Seam gaps remain a separate assembly test; they are not added as an
    invented end allowance.
    """
    schematic=source.get('schematic')
    if not schematic:return []
    model=copy.deepcopy(source['model'])
    observations={}
    for face in faces:
        if face['source_id']!=source['id'] or face.get('units')!='mm':continue
        for m in face['members']:observations.setdefault(m['id'],[]).append(m)
    corrected=set()
    for view in model['views']:
        view['scale']['mm_per_pt']=1.
        selected=[]
        for member in view['members']:
            values=observations.get(member['id'],[])
            if len(values)!=1:continue
            actual=values[0]
            if actual.get('correction') or member.get('schematic_correction'):corrected.add(member['id'])
            member.pop('schematic_correction',None)
            member['axis_pt']={'a':actual['a'],'b':actual['b']};selected.append(member)
        view['members']=selected
    rows=model_checks(model,schematic)
    for row in rows:
        row.update(source_id=source['id'],stage='assembled_model',
                   contains_corrected_members=bool(set(row['member_ids'])&corrected))
    return rows


def publish_schematic(site, report, checks=None, corrections=()):
    site=Path(site);rows=checks if checks is not None else report['rows'];esc=lambda v:html.escape(str(v))
    fmt=lambda v:'待定' if v is None else f'{v:.3f}'
    labels={'reference_only':'塔身/组合尺寸参考','pending':'待确认对应/端部基准','difference':'尺寸不一致','consistent_given_end_basis':'按当前基准 BOM 吻合',
            'within_diagnostic_bound':'模型尺寸吻合（仍需复核）'}
    body=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
          '<title>单线图整体尺寸检查</title><style>body{font:16px system-ui;margin:24px;color:#173957}a{color:#175bab;margin-right:16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd5df;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{overflow:auto}details{margin:16px 0}svg{max-width:100%;height:auto} .warn{color:#9b5200}</style>',
          '<h1>单线图整体尺寸检查</h1><nav><a href="../audit/">尺寸初审</a><a href="../model-audit/">三维尺寸复核</a><a href="template.json" download>下载件号对应模板</a><a href="report.json">完整检查数据</a></nav>',
          '<p>整体尺寸以单线图标注为准。尺寸链 = 杆件投影长度之和 + 端部伸出 − 搭接 − 退距。端套全长不一定等于有效伸出量。</p>',
          '<p class="warn">未确认的件号、尺寸端点和端部基准不会自动补差。修正使用本基准时，数值吻合不算独立验证。</p>',
          '<section><table><tr><th>检查边 / 件号</th><th>整体标注 mm</th><th>BOM 尺寸链 mm</th><th>已对应模型段及端部项 mm</th><th>差值 mm</th><th>状态</th></tr>']
    for row in rows:
        label=row['dimension'].get('scope') or '单线图 '+str(row['dimension']['value_mm'])+' mm'
        values=[label+' / '+(','.join(row.get('marks',[])) or '—'),fmt(row['dimension']['value_mm']),
                fmt(row.get('bom_chain_mm')),fmt(row.get('measured_mm')),fmt(row.get('delta_mm')),labels.get(row['status'],row['status'])]
        body.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>')
    body.append('</table></section>')
    for row in rows:
        if row['dimension'].get('formula'):body.append('<p><strong>整体基准：'+esc(row['dimension']['formula'])+'</strong></p>')
        if row.get('shared_outline_conflicts'):
            marks=sorted({v['other_mark'] for v in row['shared_outline_conflicts']})
            body.append('<p class="warn">结构图回查：件号 '+esc('/'.join(row['marks']))+' 与 '+esc('、'.join(marks))+' 共用同一条轮廓但 BOM 长度不同，须先核对关联和端部；当前差值不直接等于漏段长度。</p>')
    for vid in sorted({d['view_id'] for d in report['dimensions']}):
        body.append('<h2>单线图原图 · '+esc(vid)+'</h2><img style="max-width:100%" alt="单线图原图" src="'+esc(vid)+'.png">')
    if report.get('unresolved_numeric_labels'):
        body.append('<p>另有 '+str(len(report['unresolved_numeric_labels']))+' 个数字未确认尺寸线（可能为圈号等），未作为整体尺寸约束。</p>')
    for row in rows:
        body.append('<details><summary>'+esc(row['dimension_id'])+'：对应、端部项与计算依据</summary><pre>'+esc(__import__('json').dumps(row,ensure_ascii=False,indent=2))+'</pre></details>')
    for correction in corrections:
        body.append('<details open><summary>模型修正：'+esc(correction['member_id'])+'</summary><pre>'+esc(__import__('json').dumps(correction,ensure_ascii=False,indent=2))+'</pre></details>')
    body.append('<h2>确认件号对应并重新检查</h2><p>下载模板后填写 bindings：指定尺寸 ID、杆件观察 ID、端部贡献及原文依据。apply 为 true 时，只应用通过单线图、BOM 和原生线条检查的修正。提交会创建新结果，保留原模型和手工布局。</p><label>载入对应 JSON <input id="mapping-file" type="file" accept=".json"></label><p><label for="mapping-editor">对应记录 JSON</label></p><textarea id="mapping-editor" rows="16" style="width:100%;box-sizing:border-box" spellcheck="false"></textarea><p><button id="apply-mapping" type="button" disabled>提交对应并重新检查</button></p><p id="mapping-status" role="status"></p><script type="module" src="editor.js"></script>')
    body.append('</html>');target=site/'schematic';target.mkdir(parents=True,exist_ok=True)
    shutil.copy2(Path(__file__).parent/'schematic_web/editor.js',target/'editor.js')
    (target/'index.html').write_text('\n'.join(body),encoding='utf-8')
    if checks is not None:write_json(target/'model-check.json',{'source_sha256':report['source_sha256'],'rows':checks,'corrections':list(corrections),'engineering_accuracy_verified':False})


def add_link(path):
    path=Path(path)
    if path.exists():
        text=path.read_text(encoding='utf-8')
        if 'id="schematic-link"' not in text:
            path.write_text(text.replace('</header>','<a id="schematic-link" href="../schematic/">单线图尺寸检查</a></header>',1),encoding='utf-8')
