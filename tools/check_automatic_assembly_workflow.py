"""Upload a fresh PDF through the local UI and verify automatic assembly end to end."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urljoin

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8774/')
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--job-id',help='Resume verification of a job already uploaded by this check')
    parser.add_argument('--out',type=Path,default=Path('output/assembly-auto-11'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors,failed,external=[],[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,
            args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1650,'height':1040})
        context.on('page',lambda p:p.on('pageerror',lambda e:errors.append(str(e))))
        context.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        context.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc not in {'',urlsplit(args.url).netloc} else None)
        page=context.new_page();page.goto(args.url);page.wait_for_function('window.studioReady===true')
        if args.job_id:
            job=page.request.get(urljoin(args.url,f'api/jobs/{args.job_id}')).json()
        else:
            existing={j['id'] for j in page.request.get(urljoin(args.url,'api/jobs')).json()['jobs']}
            page.locator('#files').set_input_files(str(args.pdf.resolve()))
            with page.expect_response(lambda r:r.request.method=='POST' and urlsplit(r.url).path=='/api/jobs') as response:
                page.locator('#upload').click()
            assert response.value.status==202
            new=[j for j in page.request.get(urljoin(args.url,'api/jobs')).json()['jobs'] if j['id'] not in existing and j['name']==args.pdf.name]
            assert len(new)==1;job=new[0]
        jid=job['id'];last=None;deadline=time.monotonic()+1200
        (args.out/'uploaded-job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf8')
        while time.monotonic()<deadline:
            job=page.request.get(urljoin(args.url,f'api/jobs/{jid}'),timeout=180000).json()
            stage=(job['status'],(job.get('workflow') or {}).get('current_stage'))
            if stage!=last:print(json.dumps({'job':jid,'status':stage[0],'stage':stage[1]},ensure_ascii=False),flush=True);last=stage
            if job['status'] not in {'running','queued'}:break
            page.wait_for_timeout(2000)
        assert job['status']=='needs_review',job
        workflow=job['workflow'];assert workflow['source_sha256']==hashlib.sha256(args.pdf.read_bytes()).hexdigest()
        stages={s['id']:s['status'] for s in workflow['stages']}
        assert stages['assembly']==stages['final_check']=='completed'
        assert job['links']['assembly'] and job['links']['automaticassembly']
        (args.out/'fresh-job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf8')
        stats={}
        for name,link in [('editable',job['links']['assembly']),('saved-result',job['links']['automaticassembly'])]:
            viewer=context.new_page();viewer.goto(urljoin(args.url,link));viewer.wait_for_function('window.assemblyReady===true',timeout=30000)
            model=viewer.evaluate('assemblyData');assert model['automatic_pipeline']
            assert viewer.locator('#error').is_hidden()
            assert viewer.evaluate('assemblyView.groups.size')==len(model['faces'])
            assert viewer.evaluate('assemblyView.memberLabels.items.length')==sum(len(f['members']) for f in model['faces'])
            before=viewer.evaluate('assemblyView.camera.position.toArray()');box=viewer.locator('#assembly-canvas').bounding_box()
            x,y=box['x']+box['width']*.5,box['y']+box['height']*.5
            viewer.mouse.move(x,y);viewer.mouse.down();viewer.mouse.move(x+90,y+35,steps=10);viewer.mouse.up()
            viewer.wait_for_function('p=>assemblyView.camera.position.toArray().some((v,i)=>Math.abs(v-p[i])>1)',arg=before)
            viewer.locator('#fit').click();viewer.screenshot(path=str(args.out/(name+'-all-faces.png')))
            viewer.locator('#focus').click();viewer.screenshot(path=str(args.out/(name+'-front.png')))
            if name=='editable':
                # A second automatic action must not accumulate changes or fill history.
                revision=model['revision'];viewer.locator('#automatic-pipeline').click()
                viewer.wait_for_function('(r)=>assemblyData.revision>r&&!document.getElementById("automatic-pipeline").disabled',arg=revision,timeout=180000)
                assert viewer.evaluate('assemblyData.scene')==model['scene']
                assert viewer.evaluate('assemblyData.history')==model['history']
            viewer.reload();viewer.wait_for_function('window.assemblyReady===true',timeout=180000)
            assert viewer.evaluate('assemblyData.scene')==model['scene']
            stats[name]={'url':viewer.url,'counts':model['automatic_pipeline']['counts'],'status':model['automatic_pipeline']['status']}
            (args.out/(name+'-project.json')).write_text(json.dumps(model,ensure_ascii=False,indent=2),encoding='utf8')
            viewer.close()
        final=page.request.get(urljoin(args.url,f'results/{jid}/final/report.json')).json()
        assert any(c['id']=='assembly_dimensions' for c in final['checks'])
        assert not errors and not failed and not external,(errors,failed,external)
        result={'job_id':jid,'source_sha256':workflow['source_sha256'],'stages':stages,'views':stats,
                'errors':errors,'failed_requests':failed,'external_requests':external,
                'checks':['fresh original PDF upload','automatic pipeline stage','editable assembly created automatically','standalone result viewer','all faces/labels rendered','mouse rotation','repeat without accumulating edits','refresh persistence','assembly dimensions in final audit']}
        (args.out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False),flush=True);browser.close()


if __name__=='__main__':main()
