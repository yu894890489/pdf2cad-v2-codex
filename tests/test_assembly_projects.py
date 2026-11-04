import copy
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from tower_rebuild.assembly_projects import AssemblyProjects, AssemblyConflict
from tower_rebuild.io import write_json, file_digest


def add_source(jobs, jid, content=None, side_pairs=False):
    path=jobs.root/jid;path.mkdir(exist_ok=True)
    (path/'source.pdf').write_bytes(content or jid.encode())
    jobs.jobs[jid]={'id':jid,'name':jid+'.pdf','status':'needs_review'}
    points=[[0,0],[1000,0],[1000,1000],[0,1000]]
    members=[]
    for side in (['前','后'] if side_pairs else [None]):
        for i,(a,b) in enumerate(zip(points,points[1:]+points[:1])):
            members.append(dict(id=f'm{i}{side}',mark=str(i),side=side,axis_pt={'a':a,'b':b},
                                specification='Q345L75X6',bom_length_mm=1000,section={'legs_mm':[75,75],'thickness_mm':6}))
    model={'source_sha256':file_digest(path/'source.pdf'),'input_files':{},'views':[
        {'view_id':'v1','bbox_pt':[0,0,1000,1000],'scale':{'mm_per_pt':1,'basis':'dimension'},
         'members':members,'plates':[],'holes':[]}]}
    top=path/'run/site/topology'
    write_json(top/'model-preview.json',model)
    write_json(top/'views.json',[{'id':'v1','title':'front','page':1,'face_role':'front','image':'view-images/v1.png'}])
    write_json(top/'geometry.json',[])


@pytest.fixture
def projects(tmp_path):
    jobs=SimpleNamespace(root=tmp_path,lock=RLock(),jobs={})
    return AssemblyProjects(jobs)


def test_duplicate_pdf_deduped_and_sources_scoped(projects):
    for jid in ['one','two']:add_source(projects.jobs,jid)
    add_source(projects.jobs,'duplicate',b'one')
    p=projects.create(['one','two','duplicate'])
    d=projects.snapshot(p['id'],True)
    assert len(d['sources'])==2 and len(d['faces'])==2
    assert len({e['id'] for f in d['faces'] for e in f['edges']})==8
    assert d['duplicate_jobs']==[{'job_id':'duplicate','same_as':'one'}]


def test_split_front_rear_preserves_parallel_relation_and_no_double_count(projects):
    add_source(projects.jobs,'one',side_pairs=True)
    p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    assert len(d['faces'])==2 and len(d['parallel'])==1
    assert sum(len(f['members']) for f in d['faces'])==8
    assert not d['matches']
    assert not d['assessment']['watertight']


def test_batch_automatic_preserves_explicit_cross_document_connection(projects):
    for jid in ['one','two']:add_source(projects.jobs,jid)
    p=projects.create(['one','two']);d=projects.snapshot(p['id'],True)
    a,b=[f['edges'][0]['id'] for f in d['faces']]
    d=projects.mutate(p['id'],{'action':'connect','revision':d['revision'],'a':a,'b':b,'angle_deg':75,'flip':True})
    poses=copy.deepcopy(d['scene']['poses']);links=copy.deepcopy(d['scene']['links'])
    d=projects.mutate(p['id'],{'action':'automatic_pipeline','revision':d['revision']})
    assert d['scene']['poses']==poses and d['scene']['links']==links
    restored=AssemblyProjects(projects.jobs).snapshot(p['id'],True)
    assert restored['scene']==d['scene']


def test_manual_pose_persists_restart_append_and_revision_guards(projects):
    add_source(projects.jobs,'one');p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    fid=d['faces'][0]['id'];pose={'position':[123,456,789],'quaternion':[0,0,0,1]}
    d=projects.mutate(p['id'],{'action':'pose','revision':d['revision'],'face_id':fid,'pose':pose})
    with pytest.raises(AssemblyConflict):projects.mutate(p['id'],{'action':'arrange','revision':0})
    restored=AssemblyProjects(projects.jobs);d=restored.snapshot(p['id'],True)
    assert d['scene']['poses'][fid]==pose
    add_source(projects.jobs,'two')
    d=restored.mutate(p['id'],{'action':'add_jobs','revision':d['revision'],'job_ids':['two']})
    assert d['scene']['poses'][fid]==pose and len(d['faces'])==2
    with pytest.raises(ValueError,match='另一组'):restored.mutate(p['id'],{'action':'import','revision':d['revision'],'input_digest':'stale','scene':d['scene']})


def test_pending_sources_update_after_processing_and_source_change_is_detected(projects):
    add_source(projects.jobs,'one');projects.jobs.jobs['one']['status']='running'
    p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    assert d['waiting'] and not d['faces']
    projects.jobs.jobs['one']['status']='needs_review';d=projects.snapshot(p['id'],True)
    assert not d['waiting'] and len(d['faces'])==1
    (projects.jobs.root/'one/source.pdf').write_bytes(b'changed')
    d=projects.snapshot(p['id'],True)
    assert not d['faces'] and d['sources'][0]['error']


def test_scale_override_and_invalid_import(projects):
    add_source(projects.jobs,'one');p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    fid=d['faces'][0]['id']
    d=projects.mutate(p['id'],{'action':'scale','revision':d['revision'],'face_id':fid,'mm_per_pt':2})
    assert d['faces'][0]['edges'][0]['length']==2000
    bad=copy.deepcopy(d['scene']);bad['active_face_ids']=[fid,fid]
    with pytest.raises(ValueError,match='重复'):projects.mutate(p['id'],{'action':'import','revision':d['revision'],'input_digest':d['input_digest'],'scene':bad})


def test_preview_does_not_save_and_undo_redo_survive_restart(projects):
    add_source(projects.jobs,'one');add_source(projects.jobs,'two')
    p=projects.create(['one','two']);d=projects.snapshot(p['id'],True)
    original=copy.deepcopy(d['scene']);path=projects.root/p['id']/'project.json';before=path.read_bytes()
    a,b=[f['edges'][0]['id'] for f in d['faces']]
    args={'revision':d['revision'],'a':a,'b':b,'angle_deg':75,'flip':False}
    preview=projects.mutate(p['id'],{'action':'preview_connect',**args})
    assert preview['preview'] and preview['assessment']['links'][0]['valid']
    assert path.read_bytes()==before and projects.snapshot(p['id'],True)['scene']==original
    saved=projects.mutate(p['id'],{'action':'connect',**args})
    assert saved['scene']==preview['scene'] and saved['history']=={'undo':1,'redo':0}
    restored=AssemblyProjects(projects.jobs);d=restored.snapshot(p['id'],True)
    d=restored.mutate(p['id'],{'action':'undo','revision':d['revision']})
    assert d['scene']==original and d['history']=={'undo':0,'redo':1}
    d=restored.mutate(p['id'],{'action':'redo','revision':d['revision']})
    assert d['scene']==saved['scene'] and d['history']=={'undo':1,'redo':0}


def test_new_edit_after_undo_clears_redo_and_scale_change_clears_history(projects):
    add_source(projects.jobs,'one');p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    d=projects.mutate(p['id'],{'action':'arrange','revision':d['revision']})
    d=projects.mutate(p['id'],{'action':'undo','revision':d['revision']})
    d=projects.mutate(p['id'],{'action':'active','revision':d['revision'],'face_id':d['faces'][0]['id'],'enabled':False})
    assert d['history']=={'undo':1,'redo':0}
    d=projects.mutate(p['id'],{'action':'scale','revision':d['revision'],'face_id':d['faces'][0]['id'],'mm_per_pt':2})
    assert d['history']=={'undo':0,'redo':0}
    with pytest.raises(ValueError,match='撤销'):projects.mutate(p['id'],{'action':'undo','revision':d['revision']})


def test_undo_rejects_old_revision(projects):
    add_source(projects.jobs,'one');p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    projects.mutate(p['id'],{'action':'arrange','revision':d['revision']})
    with pytest.raises(AssemblyConflict):projects.mutate(p['id'],{'action':'undo','revision':d['revision']})


def test_failed_auto_is_visible_and_preserves_history_across_restart(projects):
    add_source(projects.jobs,'one');p=projects.create(['one']);d=projects.snapshot(p['id'],True)
    d=projects.mutate(p['id'],{'action':'arrange','revision':d['revision']})
    d=projects.mutate(p['id'],{'action':'undo','revision':d['revision']})
    before=copy.deepcopy(d['scene']);history=copy.deepcopy(d['history'])
    for _ in range(3):
        d=projects.mutate(p['id'],{'action':'auto','revision':d['revision']})
        assert not d['auto_attempt']['changed'] and d['auto_attempt']['status']=='no_solution'
        assert '未找到完整四面环' in d['auto_attempt']['message']
        assert d['history']==history and d['scene']==before
    restored=AssemblyProjects(projects.jobs).snapshot(p['id'],True)
    assert restored['auto_attempt']==d['auto_attempt']
    assert restored['missing_face_options'] and not restored['missing_face_options'][0]['ready']


def test_auto_ring_keeps_front_anchor_and_unrelated_connected_faces():
    from test_assembly import ring_faces,square
    from tower_rebuild.assembly import initial_scene,connect_faces,edge_matches,link_residual
    faces=ring_faces()+[square('U'),square('V')]
    scene=connect_faces(initial_scene(faces),faces,'U:0','V:0')
    before=copy.deepcopy(scene)
    data={'faces':faces,'matches':edge_matches(faces,[]),'parallel':[]}
    after,result=AssemblyProjects.attempt_auto(data,scene,None)
    assert result['status']=='ring_candidate'
    for fid in ['A','U','V']:assert after['poses'][fid]==before['poses'][fid]
    assert all(link in after['links'] for link in before['links'])
    edges={e['id']:e for f in faces for e in f['edges']}
    assert all(link_residual(link,edges,after['poses'])['valid'] for link in after['links'])
