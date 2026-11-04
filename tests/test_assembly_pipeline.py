import copy
from pathlib import Path

import pytest

from tower_rebuild.assembly_pipeline import run_assembly, publish_run_assembly
from tower_rebuild.io import file_digest, write_json, read_json, digest
from tower_rebuild.workflow_settings import AssemblySettings, WorkflowSettings
from test_assembly_corrections import correction_fixture
from test_assembly_projects import add_source
from types import SimpleNamespace
from threading import RLock
import numpy as np

from tower_rebuild.assembly import pose_matrix
from tower_rebuild.assembly_projects import AssemblyProjects
from tower_rebuild.assembly_missing_faces import missing_face_options


def scoped_faces(faces, parallel, source_id, repeated=False):
    cloned=copy.deepcopy(faces)
    for face in cloned:
        old=face['id'];face['id']=source_id+':'+old;face['source_id']=source_id
        for member in face['members']:member['id']=source_id+':'+member['id']
        for edge in face['edges']:
            edge['id']=source_id+':'+edge['id'];edge['face_id']=face['id']
            if edge.get('member_id'):edge['member_id']=source_id+':'+edge['member_id']
            if repeated:edge['bom_quantity']=4
    pairs=[{**p,'a':source_id+':'+p['a'],'b':source_id+':'+p['b']} for p in parallel]
    return cloned,pairs


def test_automatic_solver_infers_direction_completes_missing_face_and_keeps_bom(correction_fixture):
    faces,_,parallel,_=copy.deepcopy(correction_fixture)
    before=copy.deepcopy(faces)
    result=run_assembly([],faces=faces,parallel=parallel)
    report=result['automatic_pipeline']
    assert faces==before
    assert report['counts']['located_face_count']==5
    assert report['counts']['corrected_face_count']==1
    assert result['scene']['taper_rule']['tip_direction']=='negative_x'
    assert report['attempts'][1]['direction_basis']=='geometry'
    assert result['scene']['face_corrections']['bridge2']['summary']['member_count']==3
    assert not report['engineering_verified'] and not result['assessment']['watertight']
    repeated=run_assembly([],faces=faces,parallel=parallel)
    assert repeated['scene']==result['scene'] and repeated['faces']==result['faces']


def test_repeated_physical_marks_do_not_create_automatic_connections(correction_fixture):
    faces,_,parallel,_=copy.deepcopy(correction_fixture)
    for face in faces:
        for edge in face['edges']:edge['bom_quantity']=4
    result=run_assembly([],faces=faces,parallel=parallel)
    assert result['automatic_pipeline']['status']=='blocked'
    assert not result['scene']['links'] and not result['scene'].get('face_corrections')
    assert len(result['faces'])==len(faces)
    assert result['automatic_pipeline']['attempts'][-1]['reason']
    assert result['automatic_pipeline']['counts']['bom_differences']>0


def test_existing_manual_connections_are_preserved(correction_fixture):
    faces,_,parallel,scene=copy.deepcopy(correction_fixture)
    result=run_assembly([],faces=faces,parallel=parallel,seed_scene=scene)
    assert result['scene']==scene and result['faces']==faces
    assert result['automatic_pipeline']['attempts'][0]['preserved_existing_connections']


def test_can_disable_automatic_correction_but_still_attempt_assembly(correction_fixture):
    faces,_,parallel,_=copy.deepcopy(correction_fixture)
    result=run_assembly([],AssemblySettings(complete_missing_faces=False),faces=faces,parallel=parallel)
    assert result['scene']['links'] and not result['scene'].get('face_corrections')
    assert result['automatic_pipeline']['counts']['corrected_members']==0
    assert WorkflowSettings().assembly.enabled


def test_static_pipeline_artifacts_exist_even_when_unable_to_assemble(tmp_path):
    jobs=SimpleNamespace(root=tmp_path,jobs={});add_source(jobs,'one')
    run=tmp_path/'one/run';pdf=tmp_path/'one/source.pdf'
    write_json(run/'manifest.json',{'source':str(pdf),'source_sha256':file_digest(pdf)})
    report=publish_run_assembly(run,title='test')
    assert report['status']=='blocked' and report['counts']['face_count']==1
    assert (run/'site/assembly/index.html').is_file()
    assert 'window.ASSEMBLY_STATIC=true' in (run/'site/assembly/index.html').read_text(encoding='utf8')
    assert '"three":"./assets/three.module.js"' in (run/'site/assembly/index.html').read_text(encoding='utf8')
    assert '/assembly-assets/' not in (run/'site/assembly/assets/app.js').read_text(encoding='utf8')
    assert (run/'site/assembly/assets/three.module.js').stat().st_size>10000
    assert (run/'site/assembly/source.pdf').read_bytes()==pdf.read_bytes()
    assert read_json(run/'site/assembly/assembly.json')['automatic_pipeline']['status']=='blocked'
    model=run/'site/topology/model-preview.json';data=read_json(model);data['source_sha256']='stale';write_json(model,data)
    with pytest.raises(ValueError,match='原 PDF'):publish_run_assembly(run)


@pytest.mark.parametrize('reverse',[False,True])
def test_batch_unsolved_front_cannot_suppress_solvable_drawing(correction_fixture,reverse):
    faces,_,parallel,_=correction_fixture
    good,gp=scoped_faces(faces,parallel,'good')
    bad,bp=scoped_faces(faces,parallel,'bad',repeated=True)
    single=run_assembly([],faces=good,parallel=gp)
    batch=run_assembly([],faces=(good+bad if reverse else bad+good),parallel=bp+gp,front_face_id='bad:front')
    assert batch['automatic_pipeline']['counts']['located_face_count']==5
    assert batch['automatic_pipeline']['counts']['corrected_face_count']==1
    assert batch['scene']['front_face_id']=='good:front'
    left=single['scene']['face_corrections']['good:bridge2']['members']
    right=batch['scene']['face_corrections']['good:bridge2']['members']
    for original,combined in zip(left,right):
        assert original['mark']==combined['mark']
        for field in ('a','b','corrected_length_mm','added_mm','trimmed_mm'):
            assert np.allclose(original[field],combined[field],atol=1e-6)
    for fid in ['good:rear','good:bridge1','good:bridge2','good:bridge3']:
        # Ignore only the staging position: local geometry and relative poses
        # must match a single upload, including the inferred bottom face.
        def relative(result):
            poses=result['scene']['poses']
            return np.linalg.inv(pose_matrix(poses['good:front']))@pose_matrix(poses[fid])
        assert np.allclose(relative(single),relative(batch),atol=1e-6)
    assert not any('bad:' in l['a'] or 'bad:' in l['b'] for l in batch['scene']['links'])
    AssemblyProjects.validate_scene(batch['scene'],batch['faces'],{})


def test_multiple_successful_sources_keep_constraints_and_repeat_without_edits(correction_fixture,tmp_path,monkeypatch):
    faces,_,parallel,_=correction_fixture
    a,ap=scoped_faces(faces,parallel,'first');b,bp=scoped_faces(faces,parallel,'second')
    key=digest(['stable-source',{}])
    result=run_assembly([],faces=a+b,parallel=ap+bp,input_digest=key)
    assert result['automatic_pipeline']['counts']['located_face_count']==10
    assert result['automatic_pipeline']['counts']['corrected_face_count']==2
    assert len(result['scene']['taper_rules'])==2
    assert all(r['constraint_kind']=='taper' for r in result['assessment']['parallel_relations'])
    assert all(o['ready'] for o in missing_face_options(result['faces'],result['scene']) if o['face_id'].endswith(':bridge2'))
    repeated=run_assembly([],faces=result['faces'],parallel=ap+bp,seed_scene=result['scene'],front_face_id=result['scene']['front_face_id'],input_digest=key)
    assert repeated['scene']==result['scene']
    assert repeated['faces']==result['faces']
    assert len(repeated['automatic_pipeline']['source_results'])==2
    # A cold project load must validate every group's taper and reconstruct
    # corrections from the original faces, without rerunning/repositioning.
    jobs=SimpleNamespace(root=tmp_path,lock=RLock(),jobs={})
    projects=AssemblyProjects(jobs)
    record={'id':'restart','title':'restart','revision':1,'job_ids':[],
            'scene':result['scene'],'scene_origin':'automatic','input_digest':key,'undo':[],'redo':[]}
    projects.save(record)
    monkeypatch.setattr(AssemblyProjects,'source_data',lambda *args:([],[],'stable-source'))
    monkeypatch.setattr('tower_rebuild.assembly_projects.extract_faces',lambda *args:(copy.deepcopy(a+b),ap+bp))
    restored=AssemblyProjects(jobs).snapshot('restart',True)
    assert restored['scene']==result['scene']
    assert restored['faces']==result['faces']
