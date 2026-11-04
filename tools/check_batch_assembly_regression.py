"""Verify a real batch against its independently assembled reference drawing."""
import argparse
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import numpy as np
from playwright.sync_api import sync_playwright

from tower_rebuild.assembly import pose_matrix, transformed
from tower_rebuild.assembly_pipeline import load_run_source, run_assembly


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8774/')
    parser.add_argument('--project',required=True)
    parser.add_argument('--reference-job',required=True)
    parser.add_argument('--root',type=Path,default=Path('runs/studio-complete'))
    parser.add_argument('--out',type=Path,default=Path('output/assembly-batch-fix'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    reference=run_assembly([load_run_source(args.root/args.reference_job/'run',args.reference_job)])
    errors=[];failed=[];external=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1650,'height':1040})
        session=context.request.get(urljoin(args.url,'api/session')).json()
        endpoint=urljoin(args.url,'api/assemblies/'+args.project)
        response=context.request.get(endpoint,timeout=180000);assert response.ok,response.text()
        original=response.json()
        def act(state,action):
            response=context.request.post(endpoint,headers={'X-Upload-Token':session['token']},
                data={'revision':state['revision'],'action':action},timeout=180000)
            assert response.ok,response.text()
            return response.json()
        current=act(original,'automatic_pipeline')
        print('batch',current['automatic_pipeline']['counts'],flush=True)
        report=next(r for r in current['automatic_pipeline']['source_results'] if args.reference_job in r['source_ids'])
        assert report['counts']==reference['automatic_pipeline']['counts']
        assert report['counts']['located_face_count']>0 and report['counts']['corrected_face_count']>0
        # Measure all rod endpoints in the same front frame. This checks both
        # model geometry and its assembly placement, independent of display slots.
        def axes(data,root):
            anchor=pose_matrix(data['scene']['poses'][root]);result={}
            for face in data['faces']:
                if face['source_id']!=args.reference_job:continue
                for member in face['members']:
                    world=np.asarray(transformed([member['a'],member['b']],data['scene']['poses'][face['id']]))
                    result[(face['id'],member['id'])]=(world-anchor[:3,3])@anchor[:3,:3]
            return result
        left=axes(reference,reference['scene']['front_face_id']);right=axes(current,report['front_face_id'])
        # Only the connected group is spatially constrained; unplaced display
        # slots can vary with how many other PDFs have been uploaded.
        edge_faces={e['id']:f['id'] for f in current['faces'] for e in f['edges']}
        joined={edge_faces[l[k]] for l in current['scene']['links'] for k in ('a','b')}
        gaps=[float(np.max(np.abs(v-right[key]))) for key,v in left.items() if key[0] in joined]
        assert gaps and max(gaps)<.001,max(gaps)
        assert all(edge_faces[l['a']].split(':')[0]==edge_faces[l['b']].split(':')[0] for l in current['scene']['links'])
        saved=current
        for action in ('automatic_pipeline','auto'):
            current=act(current,action)
            assert current['scene']==saved['scene'],action
            assert current['history']==saved['history'],action
        if saved['history']['undo']:
            current=act(current,'undo')
            if saved['scene']!=original['scene']:assert current['scene']==original['scene']
            current=act(current,'redo');assert current['scene']==saved['scene']
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc not in {'',urlsplit(args.url).netloc} else None)
        page.goto(urljoin(args.url,'assemblies/'+args.project+'/'),timeout=180000)
        page.wait_for_function('window.assemblyReady===true',timeout=180000)
        assert page.evaluate('assemblyData.scene')==saved['scene']
        assert page.evaluate('assemblyView.groups.size')==len(saved['faces'])
        group=page.locator('#automatic-pipeline-details details').first
        group.locator('summary').click();group.get_by_role('button',name='查看此图装配').click()
        page.screenshot(path=str(args.out/'batch-reference-connected.png'))
        page.reload();page.wait_for_function('window.assemblyReady===true',timeout=180000)
        assert page.evaluate('assemblyData.scene')==saved['scene']
        assert not errors and not failed and not external,(errors,failed,external)
        result={'project':args.project,'reference_job':args.reference_job,'counts':saved['automatic_pipeline']['counts'],
            'reference_counts':report['counts'],'max_reference_endpoint_difference_mm':max(gaps),
            'source_groups':len(saved['automatic_pipeline']['source_results']),
            'checks':['independent single drawing matches batch','all groups attempted','no guessed cross-PDF links',
                      'automatic and shared-mark buttons repeat without edits','undo/redo','refresh','viewer group focus'],
            'errors':errors,'failed_requests':failed,'external_requests':external}
        (args.out/'batch-browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        browser.close()


if __name__=='__main__':main()
