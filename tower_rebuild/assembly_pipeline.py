"""Reusable automatic face assembly, held-out completion and final measurements.

Uses immutable drawing observations. Unresolved constraints produce an explicit
partial/blocked result with all faces available, never an invented closed box.
"""
import copy
import math
from pathlib import Path
import shutil
import numpy as np

from .assembly import (extract_faces, initial_scene, edge_matches, auto_assemble, assess_scene,
                       connected_component, pose_matrix, matrix_pose)
from .assembly_fitting import fit_tapered_faces
from .assembly_missing_faces import missing_face_candidates, missing_face_options, infer_missing_face, write_review_artifacts
from .assembly_corrections import place_correction, write_corrected_svg, validate_corrections
from .assembly_review import review_assembly
from .io import digest, read_json, write_json, file_digest
from .workflow_settings import AssemblySettings


def load_run_source(run, source_id='drawing', name=None, result_url='..', source_url='source.pdf'):
    run=Path(run);site=run/'site';manifest=read_json(run/'manifest.json')
    pdf=Path(manifest['source']);model=read_json(site/'topology/model-preview.json')
    checksum=file_digest(pdf)
    if checksum!=model['source_sha256'] or checksum!=manifest['source_sha256']:
        raise ValueError('装配输入与原 PDF 不一致，请重新生成逐面模型。')
    for key,expected in model.get('input_files',{}).items():
        path=(site/key.replace('\\','/')).resolve()
        if not path.is_relative_to(site.resolve()) or not path.is_file() or file_digest(path)!=expected:
            raise ValueError('装配输入已变化：'+key)
    verified={key.replace('\\','/') for key in model.get('input_files',{})}
    return {'id':source_id,'name':name or pdf.name,'source_sha256':checksum,'status':'needs_review',
            'source_url':source_url,'result_url':result_url,'model_available':True,'pdf_path':str(pdf),
            'model':model,'views':read_json(site/'topology/views.json'),'geometry':read_json(site/'topology/geometry.json'),
            'schematic':read_json(site/'audit/report.json').get('schematic') if 'audit/report.json' in verified else None,
            'bom':read_json(site/'tables/bom.json') if 'tables/bom.json' in verified else [],
            'member_observations':read_json(site/'topology/members.json') if 'topology/members.json' in verified else []}


def run_assembly(sources, options=None, *, input_digest=None, faces=None, parallel=None, front_face_id=None, seed_scene=None):
    options=options or AssemblySettings()
    if faces is None:faces,parallel=extract_faces(sources)
    key=input_digest or digest([(s['id'],s['model'],s.get('bom',[])) for s in sources])
    if len({f['source_id'] for f in faces})>1:
        return _run_multiple_sources(sources,options,key,faces,parallel,front_face_id,seed_scene)
    return _run_group(sources,options,key,faces,parallel,front_face_id,seed_scene)


def _run_group(sources,options,key,faces,parallel,front_face_id=None,seed_scene=None):
    raw=copy.deepcopy(faces);faces=copy.deepcopy(faces)
    matches=edge_matches(faces,parallel);edges={e['id']:e for f in faces for e in f['edges']}
    lookup={f['id']:f for f in faces};attempts=[];reports=[]
    # Same mark with BOM quantity >1 cannot identify a physical shared rod.
    safe=[m for m in matches if all(edges[e].get('bom_quantity')==1 for e in (m['a'],m['b']))
          and lookup[m['face_a']]['source_id']==lookup[m['face_b']]['source_id']]
    if seed_scene and seed_scene.get('links'):
        scene=copy.deepcopy(seed_scene);result={'found':True,'preserved_existing_connections':True}
    else:
        result=auto_assemble(faces,safe,parallel,front_face_id);scene=result.pop('scene')
    if seed_scene:
        scene['scale_overrides']=copy.deepcopy(seed_scene.get('scale_overrides',{}))
        if not seed_scene.get('links'):
            # Anchor a new group in its existing display slot, without moving
            # unrelated faces or changing any local member geometry.
            root=scene['front_face_id'];placed=set()
            if result['found']:
                placed=connected_component(root,scene['links'],edges)
                delta=pose_matrix(seed_scene['poses'][root])@np.linalg.inv(pose_matrix(scene['poses'][root]))
                for fid in placed:scene['poses'][fid]=matrix_pose(delta@pose_matrix(scene['poses'][fid]))
            for fid in set(scene['poses'])-placed:scene['poses'][fid]=copy.deepcopy(seed_scene['poses'][fid])
    attempts.append({'method':'four_face_ring',**result})
    if not result['found'] and scene['front_face_id']:
        try:
            scene=fit_tapered_faces(faces,safe,parallel,scene,options.tip_direction)
            attempts.append({'method':'multiple_shared_member_bridges','found':True,
                             'tip_direction':scene['taper_rule']['tip_direction'],
                             'direction_basis':'geometry' if options.tip_direction=='auto' else 'configuration'})
        except ValueError as exc:
            attempts.append({'method':'multiple_shared_member_bridges','found':False,'reason':str(exc)})
            if hasattr(exc, 'diagnostics'):
                attempts[-1]['seams'] = exc.diagnostics
    if options.complete_missing_faces and scene['links']:
        attempted=set()
        for _ in range(options.max_corrections):
            edge_map={e['id']:e for f in faces for e in f['edges']}
            located=connected_component(scene['front_face_id'],scene['links'],edge_map)
            candidates=[c for c in missing_face_candidates(faces,scene) if c['face_id'] not in located|attempted]
            if not candidates:break
            candidate=candidates[0];fid=candidate['face_id'];attempted.add(fid)
            target=next(f for f in raw if f['id']==fid)
            try:
                report=infer_missing_face([target if f['id']==fid else f for f in faces],scene,fid,sources,key)
            except ValueError as exc:
                reports.append({'target_face_id':fid,'title':target['title'],'source_id':target.get('source_id'),
                    'source_sha256':target.get('source_sha256'),'correction':{'status':'blocked','message':str(exc)}})
                continue
            try:
                proposed,corrected=place_correction(faces,scene,target,report,key)
                updated=[corrected if f['id']==fid else f for f in faces]
                validate_corrections(proposed,updated)
                scene,faces=proposed,updated
            except ValueError as exc:
                report['correction']={'status':'blocked','message':str(exc)}
            reports.append(report)
    return _assembly_result(sources,key,faces,raw,parallel,scene,attempts,reports)


def _run_multiple_sources(sources,options,key,faces,parallel,front_face_id,seed_scene):
    """Solve each PDF independently; retain explicit cross-PDF manual groups.

    A front face from an unrelated drawing must never suppress another PDF's
    solvable structure. Identical mark strings do not merge PDF namespaces.
    """
    raw=copy.deepcopy(faces);lookup={f['id']:f for f in faces}
    edges={e['id']:e for f in faces for e in f['edges']}
    scene=copy.deepcopy(seed_scene) if seed_scene else initial_scene(faces)
    parents={f['source_id']:f['source_id'] for f in faces}
    def find(sid):
        while parents[sid]!=sid:sid=parents[sid]
        return sid
    # User-connected PDFs are one anchored group. Never discard or reposition
    # either endpoint merely because the source documents differ.
    for link in scene['links']:
        a,b=(lookup[edges[link[k]]['face_id']]['source_id'] for k in ('a','b'))
        parents[find(b)]=find(a)
    groups={}
    for sid in sorted(parents):groups.setdefault(find(sid),set()).add(sid)
    results=[];updated={};attempts=[];reports=[];rules=[];fits={};fronts={}
    metadata={s['id']:s for s in sources}
    source_results=[]
    for source_ids in groups.values():
        subset=[f for f in faces if f['source_id'] in source_ids];ids={f['id'] for f in subset}
        relations=[p for p in parallel if p['a'] in ids and p['b'] in ids]
        requested=front_face_id if front_face_id in ids else None
        saved=(scene.get('source_fronts') or {}).get(sorted(source_ids)[0])
        root=requested or (saved if saved in ids else initial_scene(subset)['front_face_id'])
        local={'poses':{fid:copy.deepcopy(scene['poses'][fid]) for fid in ids},
               'active_face_ids':[fid for fid in scene['active_face_ids'] if fid in ids],
               'front_face_id':root,'scale_overrides':{fid:v for fid,v in scene.get('scale_overrides',{}).items() if fid in ids},
               'links':[copy.deepcopy(l) for l in scene['links'] if edges[l['a']]['face_id'] in ids and edges[l['b']]['face_id'] in ids]}
        for field in ('face_corrections',):
            values={fid:copy.deepcopy(v) for fid,v in scene.get(field,{}).items() if fid in ids}
            if values:local[field]=values
        group_rules=[r for r in scene.get('taper_rules',[]) if r['a'] in ids and r['b'] in ids]
        primary=scene.get('taper_rule')
        if primary and primary['a'] in ids and primary['b'] in ids and primary not in group_rules:group_rules.append(primary)
        if group_rules:
            local['taper_rule']=copy.deepcopy(next((r for r in group_rules if root in (r['a'],r['b'])),group_rules[0]))
            local['taper_rules']=copy.deepcopy(group_rules)
            fit=scene.get('taper_fits',{}).get(local['taper_rule']['a']) or scene.get('taper_fit')
            if fit:local['taper_fit']=copy.deepcopy(fit)
        outcome=_run_group([s for s in sources if s['id'] in source_ids],options,key,subset,relations,root,local)
        result=outcome['automatic_pipeline'];placed=outcome['scene'];results.append(outcome)
        updated.update({f['id']:f for f in outcome['faces']})
        for sid in source_ids:fronts[sid]=placed['front_face_id']
        group={'source_ids':sorted(source_ids),'source_id':sorted(source_ids)[0],
               'name':' / '.join(metadata.get(sid,{}).get('name',sid) for sid in sorted(source_ids)),
               'front_face_id':placed['front_face_id'],'status':result['status'],'counts':result['counts'],
               'attempts':result['attempts']}
        source_results.append(group)
        attempts.extend({**a,'source_ids':group['source_ids'],'source_name':group['name']} for a in result['attempts'])
        reports.extend(result['completion_reports'])
        for rule in placed.get('taper_rules',[])+([placed['taper_rule']] if placed.get('taper_rule') else []):
            if not any({r['a'],r['b']}=={rule['a'],rule['b']} for r in rules):rules.append(copy.deepcopy(rule))
        if placed.get('taper_fit') and placed.get('taper_rule'):fits[placed['taper_rule']['a']]=placed['taper_fit']
    scene['poses']={fid:p for r in results for fid,p in r['scene']['poses'].items()}
    scene['links']=[l for r in results for l in r['scene']['links']]
    scene['active_face_ids']=[fid for r in results for fid in r['scene']['active_face_ids']]
    corrections={fid:c for r in results for fid,c in r['scene'].get('face_corrections',{}).items()}
    if corrections:scene['face_corrections']=corrections
    scene['source_fronts']=fronts
    successful=[r for r in source_results if r['counts']['located_face_count']]
    # Keep an already connected user reference. Otherwise focus a successful
    # group instead of leaving the viewer on the first unsolved drawing.
    joined={edges[l[k]]['face_id'] for l in scene['links'] for k in ('a','b')}
    if scene['front_face_id'] not in joined and successful:
        scene['front_face_id']=max(successful,key=lambda r:r['counts']['located_face_count'])['front_face_id']
    if rules:
        scene['taper_rules']=rules;scene['taper_fits']=fits
        scene['taper_rule']=next((r for r in rules if scene['front_face_id'] in (r['a'],r['b'])),rules[0])
        if scene['taper_rule']['a'] in fits:scene['taper_fit']=fits[scene['taper_rule']['a']]
    result=_assembly_result(sources,key,[updated[f['id']] for f in faces],raw,parallel,scene,attempts,reports)
    result['automatic_pipeline']['source_results']=source_results
    result['automatic_pipeline']['message']=f'分组处理 {len(groups)} 组图纸，{len(successful)} 组已有局部装配。'+result['automatic_pipeline']['message']
    return result


def _assembly_result(sources,key,faces,raw,parallel,scene,attempts,reports):
    matches=edge_matches(faces,parallel);assessment=assess_scene(faces,scene,parallel)
    review=review_assembly(faces,matches,parallel,scene)
    edges={e['id']:e for f in faces for e in f['edges']}
    joined={edges[e]['face_id'] for link in scene['links'] for e in (link['a'],link['b'])}
    pending=sum(not link['valid'] for link in assessment['links'])
    assessment.update(located_face_count=len(joined),pending_connection_count=pending,
        valid_connection_count=len(assessment['links'])-pending,corrected_face_count=len(scene.get('face_corrections',{})))
    dimensions=[]
    for face in faces:
        for member in face['members']:
            length=math.dist(member['a'],member['b']);bom=member.get('bom_length_mm')
            dimensions.append({'face_id':face['id'],'member_id':member['id'],'mark':member['mark'],
                'original_length_mm':member.get('correction',{}).get('original_length_mm',length),
                'model_length_mm':length,'bom_length_mm':bom,'difference_mm':length-bom if bom else None,
                'status':'unknown' if not bom else 'difference' if abs(length-bom)>3 else 'within_diagnostic_tolerance',
                'corrected':bool(member.get('correction'))})
    status='closed_candidate' if assessment['watertight'] else 'partial_candidate' if joined else 'blocked'
    counts={'face_count':len(faces),'located_face_count':len(joined),'corrected_face_count':len(scene.get('face_corrections',{})),
            'corrected_members':sum(len(c['members']) for c in scene.get('face_corrections',{}).values()),
            'pending_connections':pending,'bom_differences':sum(d['status']=='difference' for d in dimensions)}
    message=f"自动处理：{len(faces)} 个面，已连接 {len(joined)} 个面，反算修正 {counts['corrected_face_count']} 个面；{pending} 条接缝、{counts['bom_differences']} 条杆长/BOM 差异待复核。"
    if not joined:message+=' 当前件号/轴线约束不足，未强制组装；请先核对识别，缺少对应面时补充图纸。'
    elif not assessment['watertight']:message+=' 尚未确认完整多面体闭合。'
    failures={r['target_face_id']:r['correction'].get('message') for r in reports if r.get('correction',{}).get('status')=='blocked'}
    report={'schema_version':'1.0','status':status,'message':message,'counts':counts,'attempts':attempts,
            'completion_reports':reports,'dimensions':dimensions,'unplaced_faces':[
                {**o,'reason':failures.get(o['face_id']) or (o['reason'] if not o['ready'] else '需要继续核对或已达到本次补全面数上限。')}
                for o in missing_face_options(faces,scene) if o['face_id'] not in joined],
            'engineering_verified':False,'input_digest':key}
    from .schematic_audit import assembly_checks
    report['schematic_checks']=[row for source in sources for row in assembly_checks(source,faces)]
    return {'faces':faces,'raw_faces':raw,'parallel':parallel,'matches':matches,'scene':scene,
            'assessment':assessment,'review':review,'automatic_pipeline':report,'input_digest':key}


def publish_run_assembly(run, options=None, title=None):
    run=Path(run);destination=run/'site/assembly';destination.mkdir(parents=True,exist_ok=True)
    if title is None and (run/'workflow.json').exists():title=read_json(run/'workflow.json').get('source_name')
    source=load_run_source(run,name=title)
    result=run_assembly([source],options);report=result['automatic_pipeline']
    site=run/'site'
    inputs=set(source['model'].get('input_files',{}))|{'topology/model-preview.json','topology/views.json','topology/geometry.json'}
    report.update(source_sha256=source['source_sha256'],input_files={n.replace('\\','/'):file_digest(site/n.replace('\\','/')) for n in inputs})
    for r in report['completion_reports']:
        folder='repairs/'+digest(r['target_face_id'])[:16];r['artifact_base']=folder+'/'
        write_review_artifacts(r,None,destination/folder,source_pdf=Path(source['pdf_path']))
        write_corrected_svg(r,destination/folder)
    metadata={k:v for k,v in source.items() if k not in {'model','geometry','views','bom','member_observations','pdf_path','schematic'}}
    snapshot={k:v for k,v in result.items() if k!='raw_faces'}
    snapshot.update(id='drawing',title=title or source['name'],revision=1,waiting=False,sources=[metadata],duplicate_jobs=[],
        geometry_revision=digest(result['scene'].get('face_corrections',{})),history={'undo':0,'redo':0},
        missing_face_options=missing_face_options(result['faces'],result['scene']),
        missing_face_candidates=missing_face_candidates(result['faces'],result['scene']),
        missing_face_review=next((r for r in reversed(report['completion_reports']) if r.get('correction',{}).get('status')=='applied_candidate'),None))
    write_json(destination/'assembly.json',snapshot);write_json(destination/'report.json',report)
    from .final_bom import build_final_bom, publish_final_bom, load_original_tables
    source.update(load_original_tables(run))
    publish_final_bom(build_final_bom([source], snapshot), site/'final-bom')
    shutil.copy2(source['pdf_path'],destination/'source.pdf')
    publish_viewer_assets(destination)
    return report


def publish_viewer_assets(destination):
    destination=Path(destination)
    assets=Path(__file__).parent
    shutil.copytree(assets/'assembly_web',destination/'assets',dirs_exist_ok=True)
    for filename in ('three.module.js','three.core.js','OrbitControls.js'):
        shutil.copy2(assets/'topology_web/vendor'/filename,destination/'assets'/filename)
    shutil.copy2(assets/'topology_web/member-labels.js',destination/'assets/member-labels.js')
    app=destination/'assets/app.js';app.write_text(app.read_text(encoding='utf8').replace("'/assembly-assets/","'./"),encoding='utf8')
    html=(assets/'assembly_web/index.html').read_text(encoding='utf8').replace('/assembly-assets/','./assets/')
    html=html.replace('<head>','<head><script>window.ASSEMBLY_STATIC=true;</script>')
    html=html.replace('<a href="/">← 图纸工作台</a>','<a href="../">← 查看图纸</a>')
    (destination/'index.html').write_text(html,encoding='utf8')
    # Portable result links for a saved viewer served outside the upload studio.
    links={'source':'source.pdf','automaticassembly':'./'}
    for key,path in [('finalbom','final-bom/index.html'),('topology','topology/index.html'),('final','final/index.html'),('ocr','ocr/index.html'),
                     ('tables','index.html'),('audit','audit/index.html'),('schematic','schematic/index.html'),('modelaudit','model-audit/index.html'),
                     ('bom','tables/bom.json'),('bomrows','tables/bom-observations.json'),('bomcheck','tables/bom-check.json'),
                     ('model','topology/model-preview.json')]:
        if (destination.parent/path).is_file():links[key]='../'+path.replace('index.html','')
    run=destination.parent.parent
    for key,path in [('report',run/'model-preflight.md'),('inputreport',run/'input-review.md'),('log',run.parent/'process.log')]:
        if path.is_file():shutil.copy2(path,destination/path.name);links[key]=path.name
    write_json(destination/'drawing-links.json',{'status':'needs_review','links':links})
