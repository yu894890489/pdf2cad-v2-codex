import copy
import math

import fitz
import pytest

from tower_rebuild.io import read_json, write_json, file_digest
from tower_rebuild.schematic_audit import (discover_dimensions, prepare_schematic, model_checks,
    apply_schematic_corrections, native_support, assembly_checks)


def stroke(a,b,i):
    return {'a':a,'b':b,'primitive_ids':[i]}


def inputs():
    views=[{'id':'schematic','role':'front_schematic','page':1,'bbox_pt':[0,0,150,50], 'primitive_ids':['dim','circle']}]
    geometry=[{'view_id':'schematic','strokes':[stroke([0,10],[100,10],'line'),stroke([0,0],[0,20],'left'),stroke([100,0],[100,20],'right')]},
              {'view_id':'front','strokes':[stroke([0,-10],[900,-10],'side1'),stroke([0,10],[900,10],'side2')]}]
    texts={'dim':{'id':'dim','text':'1000','page':1,'bbox_pt':[40,0,60,8],'direction':[1,0],'font_size_pt':10},
           'circle':{'id':'circle','text':'12','page':1,'bbox_pt':[20,25,30,35],'direction':[1,0],'font_size_pt':10},
           'end':{'id':'end','text':'100','page':1,'bbox_pt':[110,5,130,15],'direction':[1,0],'font_size_pt':10}}
    bom=[{'mark':'1','length_mm':900}]
    members=[{'id':'m1','mark':'1','view_id':'front','axis':{'a':[0,0],'b':[800,0]},'bom':bom[0]}]
    return views,geometry,texts,{'circle'},members,bom


@pytest.fixture
def case(tmp_path):
    pdf=tmp_path/'source.pdf'
    with fitz.open() as doc:
        doc.new_page(width=200,height=80);doc.save(pdf)
    write_json(tmp_path/'manifest.json',{'source':str(pdf),'source_sha256':file_digest(pdf)})
    args=inputs();prepare_schematic(tmp_path,*args)
    path=tmp_path/'site/schematic/mapping.json';mapping=read_json(path)
    mapping['bindings']=[{'dimension_id':'schematic_dim','member_ids':['m1'],'measurement':'x',
        'correspondence_confirmed':True,'end_basis_confirmed':True,
        'offsets':[{'kind':'end_extension','value_mm':100,'text_id':'end','confirmed':True}],
        'correction':{'member_id':'m1','anchor':'a','apply':True}}]
    write_json(path,mapping)
    report=prepare_schematic(tmp_path,*args)
    model={'views':[{'view_id':'front','scale':{'mm_per_pt':1},'members':[{'id':'m1','mark':'1',
        'axis_pt':{'a':[0,0],'b':[800,0]},'bom_length_mm':900,'section':{'legs_mm':[20,20]}}]}]}
    return tmp_path,args,report,model


def test_dimension_lines_need_witnesses_and_circle_is_not_dimension():
    views,g,texts,excluded,_,_=inputs()
    ds=discover_dimensions(views,g,texts,excluded)
    assert len(ds)==1 and ds[0]['value_mm']==1000
    assert ds[0]['a_pt']==[0,10] and ds[0]['b_pt']==[100,10]
    g[0]['strokes']=g[0]['strokes'][:2]
    assert discover_dimensions(views,g,texts,excluded)[0]['status']=='dimension_endpoints_pending'


def test_end_extension_and_native_supported_model_repair(case):
    path,args,report,model=case
    before=copy.deepcopy(model)
    row=model_checks(model,report)[0]
    assert row['measured_mm']==900 and row['delta_mm']==-100
    proposals=apply_schematic_corrections(model,report,args[1])
    assert proposals[0]['status']=='applied_candidate'
    assert proposals[0]['original_length_mm']==800 and proposals[0]['target_length_mm']==900
    assert model['views'][0]['members'][0]['axis_pt']['a']==before['views'][0]['members'][0]['axis_pt']['a']
    row=model_checks(model,report)[0]
    assert row['measured_mm']==1000 and row['original_measured_mm']==900
    assert row['reference_reused_for_correction'] and row['status']=='within_diagnostic_bound'
    assert report['rows'][0]['bom_chain_mm']==1000


@pytest.mark.parametrize('fault',['stale','unknown_member','duplicate_member','wrong_projection','no_source','negative_offset','nonfinite','duplicate_dimension'])
def test_invalid_mapping_is_rejected(case,fault):
    path,args,report,model=case
    target=path/'site/schematic/mapping.json';m=read_json(target);b=m['bindings'][0]
    if fault=='stale':m['input_sha256']='different'
    elif fault=='unknown_member':b['member_ids']=['missing']
    elif fault=='duplicate_member':b['member_ids']=['m1','m1']
    elif fault=='wrong_projection':b['measurement']='axial'
    elif fault=='no_source':b['offsets'][0]['value_mm']=99
    elif fault=='negative_offset':b['offsets'][0]['value_mm']=-100
    elif fault=='nonfinite':b['offsets'][0]['value_mm']=math.nan
    elif fault=='duplicate_dimension':m['bindings'].append(copy.deepcopy(b))
    if fault=='nonfinite':
        import json
        target.write_text(json.dumps(m),encoding='utf-8')
    else:write_json(target,m)
    with pytest.raises(ValueError):prepare_schematic(path,*args)


def test_unconfirmed_chain_cannot_repair_or_pass_model(case):
    path,args,report,model=case
    p=path/'site/schematic/mapping.json';m=read_json(p);m['bindings'][0]['end_basis_confirmed']=False;write_json(p,m)
    report=prepare_schematic(path,*args)
    assert model_checks(model,report)[0]['status']=='pending'
    assert apply_schematic_corrections(model,report,args[1])[0]['status']=='blocked'


def test_bom_conflict_is_not_absorbed_by_repair(case):
    _,args,report,model=case;report=copy.deepcopy(report)
    report['rows'][0].update(status='difference',bom_delta_mm=50)
    before=copy.deepcopy(model['views'])
    assert apply_schematic_corrections(model,report,args[1])[0]['status']=='blocked'
    assert model['views']==before


def test_missing_side_and_wrong_width_cannot_support_extension(case):
    _,args,report,model=case
    geometry=copy.deepcopy(args[1]);geometry[1]['strokes'].pop()
    assert apply_schematic_corrections(model,report,geometry)[0]['status']=='blocked'
    geometry[1]['strokes'].append(stroke([0,50],[900,50],'neighbor'))
    assert apply_schematic_corrections(model,report,geometry)[0]['status']=='blocked'


def test_overlap_is_subtracted_not_added(case):
    path,args,_,model=case;p=path/'site/schematic/mapping.json';m=read_json(p);b=m['bindings'][0]
    b['offsets'][0]['kind']='overlap';write_json(p,m)
    report=prepare_schematic(path,*args)
    assert report['rows'][0]['bom_chain_mm']==800
    assert model_checks(model,report)[0]['measured_mm']==700


def test_projection_is_recomputed_from_model_geometry(case):
    _,_,report,model=case
    model['views'][0]['members'][0]['axis_pt']['b']=[600,800]
    assert model_checks(model,report)[0]['measured_mm']==700  # 600 horizontal + 100 end.


def test_final_assembly_remeasures_corrected_geometry_and_keeps_source_scope(case):
    _,_,report,model=case;source={'id':'sheet9','model':model,'schematic':report}
    faces=[{'source_id':'sheet9','units':'mm','members':[{'id':'m1','a':[0,0,0],'b':[950,0,0],'correction':{'applied':True}}]},
           {'source_id':'sheet10','units':'mm','members':[{'id':'m1','a':[0,0,0],'b':[2000,0,0]}]}]
    rows=assembly_checks(source,faces)
    assert rows[0]['measured_mm']==1050 and rows[0]['status']=='difference'
    assert rows[0]['contains_corrected_members']
    faces[0]['units']='pdf_pt'
    assert assembly_checks(source,faces)[0]['status']=='pending'


def test_review_submission_creates_separate_job_and_rejects_stale_mapping(case):
    import shutil
    from threading import RLock
    from tower_rebuild.studio import JobStore
    path,args,report,model=case
    root=path/'jobs';(root/'old/run/site/schematic').mkdir(parents=True)
    shutil.copy2(path/'source.pdf',root/'old/source.pdf')
    shutil.copy2(path/'site/schematic/report.json',root/'old/run/site/schematic/report.json')
    original=(root/'old/source.pdf').read_bytes()
    mapping=read_json(path/'site/schematic/mapping.json')
    store=JobStore.__new__(JobStore);store.root=root;store.lock=RLock();store.closing=False
    store.jobs={'old':{'name':'drawing.pdf','status':'needs_review'}}
    store.submit=lambda jid,name,retry_of:{'id':jid,'retry_of':retry_of}
    created=store.review_schematic('old',mapping)
    assert created['retry_of']=='old' and created['id']!='old'
    assert read_json(root/created['id']/'schematic-map.json')==mapping
    assert (root/'old/source.pdf').read_bytes()==original
    before=set(root.iterdir());mapping['input_sha256']='stale'
    with pytest.raises(ValueError,match='过期'):store.review_schematic('old',mapping)
    assert set(root.iterdir())==before


def test_structure_check_reports_native_missing_span_without_applying(case):
    from tower_rebuild.schematic_audit import diagnose_structure
    _,args,report,model=case;before=copy.deepcopy(model)
    findings=diagnose_structure(model,report,args[1])
    assert findings[0]['missing_or_excess_mm']==100
    assert findings[0]['diagnosis']=='native_supported_extent_candidate'
    assert model==before


def test_user_composite_dimension_excludes_body_and_keeps_unknown_ends_pending(case):
    path,args,_,model=case
    views,geometry,texts,excluded,members,bom=args
    texts['dim']['text']='7000'
    for index,(tid,value) in enumerate([('body','3364'),('tip','480')],1):
        y=30*index
        views[0]['primitive_ids'].append(tid)
        texts[tid]={**texts['dim'],'id':tid,'text':value,'bbox_pt':[40,y,60,y+8]}
        geometry[0]['strokes'] += [stroke([0,10+y],[100,10+y],tid+'line'),stroke([0,y],[0,20+y],tid+'left'),stroke([100,y],[100,20+y],tid+'right')]
    mapping_path=path/'site/schematic/mapping.json';mapping_path.unlink()
    prepare_schematic(path,*args);mapping=read_json(mapping_path)
    mapping['derived_dimensions']=[{'id':'derived_lower','measurement':'x','scope':'横担下边，排除塔身',
        'terms':[{'dimension_id':'schematic_dim','factor':1},{'dimension_id':'schematic_body','factor':.5},{'dimension_id':'schematic_tip','factor':.5}]}]
    mapping['bindings']=[{'dimension_id':'derived_lower','member_ids':['m1'],'measurement':'x','correspondence_confirmed':True,'end_basis_confirmed':False}]
    write_json(mapping_path,mapping)
    report=prepare_schematic(path,*args);row=model_checks(model,report)[0]
    assert row['reference_mm']==8922 and row['measured_mm']==800
    assert row['status']=='pending' and row['delta_mm']==-8122
    assert row['dimension']['formula']=='7000 × 1 + 3364 × 0.5 + 480 × 0.5 = 8922 mm'
    assert all(r['status']=='reference_only' for r in model_checks(model,report)[1:])
    mapping['derived_dimensions'][0]['terms'][0]['dimension_id']='arbitrary-expression'
    write_json(mapping_path,mapping)
    with pytest.raises(ValueError):prepare_schematic(path,*args)
