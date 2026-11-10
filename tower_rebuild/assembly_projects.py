"""Persistent local assembly projects referencing independently parsed PDFs."""
import copy
from pathlib import Path
import uuid

import numpy as np

from .assembly import (extract_faces, initial_scene, edge_matches, auto_assemble, assess_scene,
                       connect_faces, connected_component, pose_matrix, matrix_pose)
from .assembly_review import review_assembly
from .assembly_fitting import fit_tapered_faces, validate_taper_rule
from .assembly_corrections import (apply_face_correction, place_correction, validate_corrections,
                                   write_corrected_svg)
from .assembly_missing_faces import (missing_face_candidates, missing_face_options, infer_missing_face,
                                     review_signature, write_review_artifacts)
from .io import read_json, write_json, file_digest, digest
from .workflow import now
from .assembly_pipeline import run_assembly
from .workflow_settings import AssemblySettings


class AssemblyConflict(ValueError):
    pass


def layout_key(value):
    """Ignore numerical roundoff when a rigid solver repeats an identical pose."""
    if isinstance(value,float):return round(value,7)
    if isinstance(value,dict):return {k:layout_key(v) for k,v in value.items()}
    if isinstance(value,list):return [layout_key(v) for v in value]
    return value


class AssemblyProjects:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = jobs.root / 'assemblies'
        self.records = {}
        self.cache = {}
        self.original_faces = {}
        for path in self.root.glob('*/project.json'):
            record = read_json(path)
            if record['id'] == path.parent.name:
                self.records[record['id']] = record

    def save(self, record):
        record['updated_at'] = now()
        write_json(self.root / record['id'] / 'project.json', record)

    def validate_jobs(self, ids):
        if not isinstance(ids, list) or not ids or len(ids)>30 or any(not isinstance(j,str) or j not in self.jobs.jobs for j in ids):
            raise ValueError('请选择 1 至 30 个已上传图纸任务。')
        selected, duplicates, hashes = [], [], {}
        for jid in dict.fromkeys(ids):
            checksum = file_digest(self.jobs.root / jid / 'source.pdf')
            if checksum in hashes:
                duplicates.append({'job_id':jid, 'same_as':hashes[checksum]})
            else:
                hashes[checksum]=jid;selected.append(jid)
        return selected, duplicates

    def create(self, job_ids, title='横担层装配'):
        with self.jobs.lock:
            ids,duplicates = self.validate_jobs(job_ids)
            if not isinstance(title,str) or not title.strip() or len(title)>120:
                raise ValueError('装配名称须为 1 至 120 个字符。')
            record = {'schema_version':'1.0', 'id':uuid.uuid4().hex, 'title':title.strip(),
                      'job_ids':ids,'duplicate_jobs':duplicates,'created_at':now(),'revision':0,
                      'scene':None,'input_digest':None,'scene_origin':'automatic'}
            self.records[record['id']] = record
            self.save(record)
            return self.snapshot(record['id'])

    def source_data(self, record):
        sources, summary, signatures = [], [], []
        for jid in record['job_ids']:
            job = self.jobs.jobs[jid]; run = self.jobs.root / jid / 'run'; top = run / 'site/topology'
            info = {'id':jid,'name':job['name'],'status':job['status'], 'model_available':False,
                    'source_url':f'/api/jobs/{jid}/source','result_url':f'/results/{jid}'}
            if hasattr(self.jobs,'snapshot'):
                info['links']=self.jobs.snapshot(jid)['links']
            signature = {'id':jid,'status':job['status']}
            paths = [top/name for name in ('model-preview.json','views.json','geometry.json')]
            if job['status'] not in {'queued','running'} and all(p.is_file() for p in paths):
                model,views,geometry = (read_json(p) for p in paths)
                checksum = file_digest(self.jobs.root / jid / 'source.pdf')
                if model.get('source_sha256') != checksum:
                    info['error']='模型与原 PDF 不一致，请重新处理该图纸。'
                else:
                    invalid=[]
                    for name,value in model.get('input_files',{}).items():
                        root=(run/'site').resolve();path=(root/name).resolve()
                        if not path.is_relative_to(root) or not path.is_file() or file_digest(path)!=value:
                            invalid.append(name)
                    if invalid:
                        info['error']='逐面模型输入已变化，请重新生成三维预览。'
                    else:
                        info['model_available']=True;info['source_sha256']=checksum
                        bom_path=run/'site/tables/bom.json'
                        # Only consume a BOM covered by the model's verified input hashes.
                        bom_verified=any(name.replace('\\','/')=='tables/bom.json' for name in model.get('input_files',{}))
                        members_path=top/'members.json'
                        members_verified=any(name.replace('\\','/')=='topology/members.json' for name in model.get('input_files',{}))
                        sources.append({**info,'model':model,'views':views,'geometry':geometry,
                                        'schematic':read_json(run/'site/audit/report.json').get('schematic') if 'audit/report.json' in {n.replace('\\','/') for n in model.get('input_files',{})} else None,
                                        'pdf_path':str(self.jobs.root/jid/'source.pdf'),
                                        'member_observations':read_json(members_path) if members_verified and members_path.is_file() else [],
                                        'bom':read_json(bom_path) if bom_verified and bom_path.is_file() else []})
                signature['files']=[file_digest(p) for p in paths]
                signature['source_sha256']=checksum
                signature['error']=info.get('error')
            summary.append(info);signatures.append(signature)
        return sources, summary, digest(signatures)

    def refresh(self, aid):
        record = self.records[aid]
        sources, summary, source_hash = self.source_data(record)
        overrides = (record.get('scene') or {}).get('scale_overrides',{})
        key = digest([source_hash,overrides])
        cached = self.cache.get(aid)
        if cached and record['input_digest']==key:
            return cached
        faces,parallel = extract_faces(sources,overrides)
        input_changed=record.get('input_digest')!=key
        self.original_faces[aid]=copy.deepcopy(faces)
        if input_changed:
            record['undo']=[];record['redo']=[]
            record.pop('auto_attempt',None);record.pop('tip_directions',None)
        matches = edge_matches(faces,parallel)
        defaults = initial_scene(faces)
        waiting = any(s['status'] in {'queued','running'} for s in summary)
        previous = record.get('scene')
        auto = {'found':False,'tested':0,'search_limited':False}
        if previous:
            scene=copy.deepcopy(previous)
            if input_changed:
                scene.pop('face_corrections',None)
                scene['links']=[link for link in scene['links'] if link.get('basis')!='inferred_face_correction']
            ids={f['id'] for f in faces};edges={e['id']:e for f in faces for e in f['edges']}
            new=set(ids)-set(scene['poses'])
            scene['poses']={fid:scene['poses'].get(fid,pose) for fid,pose in defaults['poses'].items()}
            scene['active_face_ids']=[fid for fid in scene['active_face_ids'] if fid in ids]+[fid for fid in defaults['active_face_ids'] if fid in new]
            scene['links']=[link for link in scene['links'] if link['a'] in edges and link['b'] in edges]
            try:
                validate_taper_rule(scene.get('taper_rule'),{e['id']:e for f in faces for e in f['edges']},ids)
            except ValueError:
                scene.pop('taper_rule',None);scene.pop('taper_fit',None)
            if 'source_fronts' in scene:
                scene['source_fronts']={sid:fid for sid,fid in scene['source_fronts'].items() if fid in ids}
            if 'taper_rules' in scene:
                valid=[]
                for rule in scene['taper_rules']:
                    try:validate_taper_rule(rule,edges,ids)
                    except ValueError:continue
                    valid.append(rule)
                scene['taper_rules']=valid
                scene['taper_fits']={fid:fit for fid,fit in scene.get('taper_fits',{}).items() if fid in ids}
            if scene.get('front_face_id') not in ids:scene['front_face_id']=defaults['front_face_id']
            # Uploading starts with no models. Once parsing finishes, make the
            # first automatic attempt unless the user already edited placement.
            if faces and not waiting and input_changed and record.get('scene_origin')=='automatic':
                outcome=self.automatic_pipeline(record,sources,faces,parallel,key,scene['front_face_id'])
                scene=outcome['scene'];auto={'found':bool(scene['links']),'method':'automatic_pipeline'}
        else:
            if faces and not waiting:
                outcome=self.automatic_pipeline(record,sources,faces,parallel,key)
                scene=outcome['scene'];auto={'found':bool(scene['links']),'method':'automatic_pipeline'}
            else:scene=defaults
        scene['scale_overrides']=overrides
        record.update(scene=scene,input_digest=key,source_digest=source_hash,revision=record['revision']+1)
        data={'schema_version':'1.0','id':aid,'title':record['title'],'revision':record['revision'],
              'input_digest':key,'sources':summary,'faces':faces,'parallel':parallel,'matches':matches,
              'scene':scene,'auto_result':auto,'waiting':waiting,'duplicate_jobs':record.get('duplicate_jobs',[]),
              'engineering_verified':False}
        self.cache[aid]=data
        return self.persist(record,data)

    def persist(self, record, data):
        data.update(revision=record['revision'],scene=record['scene'])
        raw=self.original_faces.setdefault(record['id'],copy.deepcopy(data['faces']))
        corrections=record['scene'].get('face_corrections',{})
        effective=[]
        for face in raw:
            correction=corrections.get(face['id'])
            effective.append(apply_face_correction(face,correction,data.get('input_digest')) if correction else copy.deepcopy(face))
        data['faces']=effective
        data['matches']=edge_matches(effective,data['parallel'])
        data['geometry_revision']=digest([data.get('input_digest'),corrections])
        data['assessment']=assess_scene(data['faces'],record['scene'],data['parallel'])
        data['assessment']['corrected_face_count']=len(corrections)
        data['review']=review_assembly(data['faces'],data['matches'],data['parallel'],record['scene'])
        data['missing_face_candidates']=missing_face_candidates(data['faces'],record['scene'])
        data['missing_face_options']=missing_face_options(data['faces'],record['scene'])
        missing=record.get('missing_face_review')
        current=missing and missing['basis_signature']==review_signature(data.get('input_digest'),record['scene'])
        data['missing_face_review']=missing if current else None
        data['missing_face_review_stale']=bool(missing and not current)
        data['missing_face_selected_id']=missing.get('target_face_id') if missing else None
        pipeline=record.get('automatic_pipeline')
        data['automatic_pipeline']=pipeline if pipeline and pipeline['basis_signature']==review_signature(data.get('input_digest'),record['scene']) else None
        # Remember explicit tip choices even when "arrange" clears all links.
        directions=record.setdefault('tip_directions',{})
        for previous in reversed(record.get('undo',[])):
            rule=previous.get('taper_rule')
            if rule and rule.get('tip_direction') in {'negative_x','positive_x'}:
                directions.setdefault(rule['a'],rule['tip_direction'])
        rule=record['scene'].get('taper_rule')
        if rule:directions[rule['a']]=rule['tip_direction']
        data['tip_direction_hint']=directions.get(record['scene'].get('front_face_id'))
        attempt=record.get('auto_attempt')
        data['auto_attempt']=attempt if attempt and attempt['basis_signature']==review_signature(data.get('input_digest'),record['scene']) else None
        edge_faces={e['id']:f['id'] for f in data['faces'] for e in f['edges']}
        joined={edge_faces[e] for link in data['assessment']['links'] for e in (link['a'],link['b'])}
        data['assessment']['located_face_count']=len(joined)
        pending=sum(not link['valid'] for link in data['assessment']['links'])
        data['assessment']['pending_connection_count']=pending
        data['assessment']['valid_connection_count']=len(data['assessment']['links'])-pending
        if data['assessment']['status']=='needs_more_faces_or_manual' and data['review']['blockers']:
            prefix=f'已局部连接 {len(joined)} 个面，{pending} 条接缝尺寸待复核；' if joined else '现有模型'
            data['assessment']['message']=prefix+'尚不能确认闭合。请先核对识别缺项和几何冲突；本图缺少对应结构时再补图。'
        data['history']={'undo':len(record.get('undo',[])),'redo':len(record.get('redo',[]))}
        data['status']='waiting_for_drawings' if data['waiting'] else data['assessment']['status']
        self.save(record)
        write_json(self.root/record['id']/'assembly.json',data)
        return data

    def automatic_pipeline(self,record,sources,faces,parallel,key,front=None,seed_scene=None):
        options=getattr(getattr(self.jobs,'config',None),'assembly',AssemblySettings())
        if not options.enabled:
            return {'scene':copy.deepcopy(seed_scene) if seed_scene else initial_scene(faces)}
        outcome=run_assembly(sources,options,input_digest=key,faces=faces,parallel=parallel,front_face_id=front,seed_scene=seed_scene)
        report=outcome['automatic_pipeline'];report['basis_signature']=review_signature(key,outcome['scene'])
        record['automatic_pipeline']=report
        completed=[r for r in report['completion_reports'] if r.get('correction',{}).get('status')=='applied_candidate']
        if completed:
            latest=copy.deepcopy(completed[-1]);latest['basis_signature']=report['basis_signature']
            latest['artifact_key']=digest([key,outcome['scene'],latest['target_face_id'],'correction-v1'])
            write_review_artifacts(latest,self.jobs.root,self.root/record['id'])
            write_corrected_svg(latest,self.root/record['id'])
            record['missing_face_review']=latest
        return outcome

    def snapshot(self, aid, full=False):
        with self.jobs.lock:
            data=self.refresh(aid)
            if full:return copy.deepcopy(data)
            return {k:data[k] for k in ('id','title','revision','status','waiting','sources','assessment','duplicate_jobs')} | {
                'face_count':len(data['faces']),'url':f'/assemblies/{aid}/','updated_at':self.records[aid]['updated_at']}

    def listing(self):
        with self.jobs.lock:
            return [self.snapshot(aid) for aid in sorted(self.records,key=lambda a:self.records[a]['created_at'],reverse=True)]

    def export_final_bom(self, aid):
        from .final_bom import build_final_bom, load_original_tables, publish_final_bom
        with self.jobs.lock:
            # Effective faces already contain corrections and current scale overrides.
            # Reading an export never reruns the solver or changes saved placement.
            snapshot = self.snapshot(aid, full=True)
            sources, _, _ = self.source_data(self.records[aid])
            for source in sources:
                source.update(load_original_tables(self.jobs.root/source['id']/'run'))
            return publish_final_bom(build_final_bom(sources, snapshot), self.root/aid/'final-bom', parts_url='../part-drawings/')

    def mutate(self, aid, body):
        with self.jobs.lock:
            data=self.refresh(aid);record=self.records[aid]
            if body.get('revision')!=record['revision']:
                raise AssemblyConflict('装配已在其他窗口或图纸处理完成后更新，请刷新后重试。')
            action=body.get('action');faces=data['faces'];lookup={f['id']:f for f in faces}
            edges={e['id']:e for f in faces for e in f['edges']}
            scene=copy.deepcopy(record['scene'])
            before=copy.deepcopy(scene)
            if action=='review_missing_face':
                sources,_,_=self.source_data(record)
                target=next((f for f in self.original_faces[aid] if f['id']==body.get('face_id')),None)
                if target is None:raise ValueError('未找到待修正结构面。')
                analysis_faces=[target if f['id']==target['id'] else f for f in faces]
                report=infer_missing_face(analysis_faces,scene,target['id'],sources,data['input_digest'])
                try:
                    proposed,corrected=place_correction(faces,scene,target,report,data['input_digest'])
                    self.validate_scene(proposed,[corrected if f['id']==target['id'] else f for f in faces],scene['scale_overrides'])
                except ValueError as exc:
                    proposed=scene;report['correction']={'status':'blocked','message':str(exc)}
                    report['issues'].append('未应用模型修正：'+str(exc))
                report['basis_signature']=review_signature(data['input_digest'],proposed)
                report['artifact_key']=digest([data['input_digest'],proposed,target['id'],'correction-v1'])
                write_review_artifacts(report,self.jobs.root,self.root/aid)
                write_corrected_svg(report,self.root/aid)
                if layout_key(proposed)!=layout_key(scene):
                    record.setdefault('undo',[]).append(before);record['undo']=record['undo'][-30:];record['redo']=[]
                record.update(scene=proposed,scene_origin='manual',missing_face_review=report,revision=record['revision']+1)
                return copy.deepcopy(self.persist(record,data))
            if action=='add_jobs':
                ids,duplicates=self.validate_jobs(record['job_ids']+body.get('job_ids',[]))
                record['job_ids']=ids;record['duplicate_jobs']=duplicates
                record['input_digest']=None;self.cache.pop(aid,None);self.save(record)
                return self.snapshot(aid,full=True)
            if action=='preview_connect':
                preview=connect_faces(scene,faces,body['a'],body['b'],float(body.get('angle_deg',90)),bool(body.get('flip',True)))
                self.validate_scene(preview,faces,scene['scale_overrides'])
                return {'revision':record['revision'],'input_digest':data['input_digest'],'preview':True,
                        'scene':preview,'assessment':assess_scene(faces,preview,data['parallel'])}
            if action in {'preview_taper','fit_taper'}:
                proposed=fit_tapered_faces(faces,data['matches'],data['parallel'],scene,body.get('tip_direction'))
                self.validate_scene(proposed,faces,scene['scale_overrides'])
                if action=='preview_taper':
                    return {'revision':record['revision'],'input_digest':data['input_digest'],'preview':True,
                            'scene':proposed,'assessment':assess_scene(faces,proposed,data['parallel'])}
            if action in {'undo','redo'}:
                stack=record.get(action,[])
                if not stack:raise ValueError('没有可以撤销或重做的装配操作。')
                scene=copy.deepcopy(stack[-1])
                self.validate_scene(scene,faces,before['scale_overrides'])
                stack.pop();record.setdefault('redo' if action=='undo' else 'undo',[]).append(before)
            elif action=='fit_taper':
                scene=proposed
            elif action=='connect':
                scene=connect_faces(scene,faces,body['a'],body['b'],float(body.get('angle_deg',90)),bool(body.get('flip',True)))
            elif action=='pose':
                fid=body['face_id']
                if fid not in lookup:raise ValueError('未找到需要移动的结构面。')
                destination=pose_matrix(body['pose'])
                delta=destination@np.linalg.inv(pose_matrix(scene['poses'][fid]))
                moving=connected_component(fid,scene['links'],edges) if body.get('move_connected',True) else {fid}
                for other in moving:scene['poses'][other]=matrix_pose(delta@pose_matrix(scene['poses'][other]))
            elif action=='unlink':
                scene['links']=[link for link in scene['links'] if link['id']!=body['link_id']]
            elif action=='active':
                fid=body['face_id']
                if fid not in lookup:raise ValueError('未找到该结构面。')
                selected=set(scene['active_face_ids'])
                if body.get('enabled'):selected.add(fid)
                else:selected.discard(fid)
                scene['active_face_ids']=sorted(selected)
            elif action=='front':
                if body['face_id'] not in lookup:raise ValueError('未找到该正视面。')
                scene['front_face_id']=body['face_id']
            elif action=='scale':
                if body['face_id'] not in lookup:raise ValueError('未找到该结构面。')
                value=float(body['mm_per_pt'])
                if not np.isfinite(value) or not .0001<=value<=1e5:raise ValueError('请输入有效的 mm/pt 比例。')
                scene['scale_overrides'][body['face_id']]=value
                record.update(scene=scene,scene_origin='manual',input_digest=None)
                self.cache.pop(aid,None)
                return self.snapshot(aid,full=True)
            elif action=='automatic_pipeline':
                sources,_,_=self.source_data(record)
                outcome=self.automatic_pipeline(record,sources,faces,data['parallel'],data['input_digest'],scene['front_face_id'],scene)
                scene=outcome['scene']
                if layout_key(scene)==layout_key(before):
                    record.update(revision=record['revision']+1)
                    return copy.deepcopy(self.persist(record,data))
            elif action=='auto':
                direction=body.get('tip_direction') or data.get('tip_direction_hint')
                if direction not in {None,'negative_x','positive_x'}:raise ValueError('尖端方向无效。')
                scene,attempt=self.attempt_auto(data,scene,direction)
                if direction:record.setdefault('tip_directions',{})[scene['front_face_id']]=direction
                # Failed attempts are visible results, not edits to the layout.
                changed=layout_key(scene)!=layout_key(before)
                if not changed:scene=before
                attempt.update(changed=changed,basis_signature=review_signature(data['input_digest'],scene))
                record['auto_attempt']=attempt
                if not changed:
                    record.update(revision=record['revision']+1,scene_origin='manual')
                    return copy.deepcopy(self.persist(record,data))
            elif action=='arrange':
                defaults=initial_scene(faces);defaults['scale_overrides']=scene['scale_overrides']
                if scene.get('face_corrections'):defaults['face_corrections']=copy.deepcopy(scene['face_corrections'])
                scene=defaults
            elif action=='import':
                if body.get('input_digest')!=data['input_digest']:raise ValueError('布局文件对应另一组图纸或比例，请使用当前项目的布局。')
                candidate=body.get('scene',{})
                self.validate_scene(candidate,faces,scene['scale_overrides'])
                for raw in self.original_faces[aid]:
                    correction=candidate.get('face_corrections',{}).get(raw['id'])
                    if correction:apply_face_correction(raw,correction,data['input_digest'])
                scene=copy.deepcopy(candidate)
            else:
                raise ValueError('未知装配操作。')
            self.validate_scene(scene,faces,scene['scale_overrides'])
            if action not in {'undo','redo'}:
                record.setdefault('undo',[]).append(before)
                record['undo']=record['undo'][-30:];record['redo']=[]
            record.update(scene=scene,revision=record['revision']+1,scene_origin='manual')
            return copy.deepcopy(self.persist(record,data))

    @staticmethod
    def attempt_auto(data, scene, direction):
        faces=data['faces'];front=scene['front_face_id']
        if len({f.get('source_id') for f in faces})>1:
            outcome=run_assembly([],AssemblySettings(tip_direction=direction or 'auto',complete_missing_faces=False),
                input_digest=data.get('input_digest'),faces=faces,parallel=data['parallel'],front_face_id=front,seed_scene=scene)
            report=outcome['automatic_pipeline'];count=report['counts']['located_face_count']
            attempt={'found':bool(count),'method':'per_source_shared_members','status':report['status'],
                     'message':report['message'],'source_results':report['source_results']}
            data['auto_result']=attempt
            return outcome['scene'],attempt
        auto=auto_assemble(faces,data['matches'],data['parallel'],front)
        proposed=auto.pop('scene');data['auto_result']=auto
        result={**auto,'method':'four_face_ring','status':'no_solution','message':''}
        if auto['found']:
            edges={e['id']:e for f in faces for e in f['edges']}
            fitted=connected_component(front,proposed['links'],edges)
            pairs={frozenset((link['a'],link['b'])) for link in proposed['links']}
            conflicting=[link for link in scene['links'] if any(edges[link[k]]['face_id'] in fitted for k in ('a','b'))
                         and frozenset((link['a'],link['b'])) not in pairs]
            if conflicting:
                result.update(found=False,message='找到四面环候选，但会影响已有手动连接。请先解除冲突连接或使用连接预览。')
                return scene,result
            aligned=copy.deepcopy(scene)
            delta=pose_matrix(scene['poses'][front])@np.linalg.inv(pose_matrix(proposed['poses'][front]))
            for fid in fitted:aligned['poses'][fid]=matrix_pose(delta@pose_matrix(proposed['poses'][fid]))
            aligned['poses'][front]=copy.deepcopy(scene['poses'][front])
            aligned['links']=[link for link in scene['links'] if not any(edges[link[k]]['face_id'] in fitted for k in ('a','b'))]+proposed['links']
            aligned['active_face_ids']=sorted(set(scene['active_face_ids'])|fitted)
            if aligned.get('taper_rule',{}).get('a') in fitted:
                aligned.pop('taper_rule',None);aligned.pop('taper_fit',None)
            result.update(status='ring_candidate',message=f'已按公共件号放置 {len(fitted)} 个面，形成四侧面环候选；完整闭合与尺寸见装配检查。')
            return aligned,result
        if direction:
            try:
                proposed=fit_tapered_faces(faces,data['matches'],data['parallel'],scene,direction)
            except ValueError as exc:
                result['message']='未找到完整四面环；局部收口装配未完成：'+str(exc)
            else:
                count=proposed['taper_fit']['face_count']
                result.update(found=True,method='tapered_shared_members',status='partial_candidate',
                              message=f'已按公共件号完成 {count} 个面的局部收口定位。尚未确认整个横担闭合；接缝超差继续列出，可继续反算缺失面。')
                return proposed,result
        else:
            shared=sum(bool(m.get('shared_member_candidate')) for m in data['matches'])
            result.update(status='needs_tip_direction' if shared and data['parallel'] else 'no_solution',
                          message=f'未找到完整四面环，共有 {shared} 对共享定位杆件。'+('请在“横向收口校正”选择尖端方向后再次尝试，或预览单条公共杆连接。' if shared and data['parallel'] else '请核对公共件号、比例和长度，或手动连接对应边。'))
        result['message']+=' 当前布局保持原值。'
        return scene,result

    @staticmethod
    def validate_scene(scene, faces, scales):
        ids={f['id'] for f in faces};edges={e['id']:e for f in faces for e in f['edges']}
        if not isinstance(scene,dict) or not isinstance(scene.get('poses'),dict) or not isinstance(scene.get('active_face_ids'),list):
            raise ValueError('布局必须包含位置对象和参与面数组。')
        if len(scene['active_face_ids'])!=len(set(scene['active_face_ids'])):
            raise ValueError('参与面不能重复。')
        if set(scene.get('poses',{}))!=ids or not set(scene.get('active_face_ids',[]))<=ids:
            raise ValueError('布局中的面与当前图纸不匹配。')
        if scene.get('front_face_id') is not None and scene['front_face_id'] not in ids:
            raise ValueError('正视面不存在。')
        if scene.get('scale_overrides')!=scales:
            raise ValueError('请用面比例设置修改比例。')
        for pose in scene['poses'].values():pose_matrix(pose)
        validate_corrections(scene,faces)
        validate_taper_rule(scene.get('taper_rule'),edges,ids)
        if not isinstance(scene.get('taper_rules',[]),list):raise ValueError('分组收口约束无效。')
        for rule in scene.get('taper_rules',[]):validate_taper_rule(rule,edges,ids)
        fronts=scene.get('source_fronts',{})
        if not isinstance(fronts,dict) or any(fid is not None and fid not in ids for fid in fronts.values()):
            raise ValueError('分组正视面不存在。')
        used=set()
        if not isinstance(scene.get('links'),list) or len(scene['links'])>len(edges):raise ValueError('连接数量无效。')
        for link in scene['links']:
            if not isinstance(link,dict) or not isinstance(link.get('flip'),bool) or not isinstance(link.get('id'),str):
                raise ValueError('连接记录无效。')
            a,b=link.get('a'),link.get('b');angle=link.get('angle_deg')
            if a not in edges or b not in edges or edges[a]['face_id']==edges[b]['face_id'] or a in used or b in used:
                raise ValueError('连接引用不存在、重复使用或属于同一面的边。')
            if not isinstance(angle,(int,float)) or not np.isfinite(angle) or not -180<=angle<=180:
                raise ValueError('连接角度无效。')
            used.update((a,b))
