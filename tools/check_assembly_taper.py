"""Browser check of local taper fitting; --apply explicitly saves one correction."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit, urljoin

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--tip-direction',choices=['negative_x','positive_x'],required=True)
    parser.add_argument('--out',type=Path,default=Path('output/assembly-taper-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors=[];failed=[];external=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1700,'height':1060})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
        page.goto(args.url);page.wait_for_function('window.assemblyReady===true')
        before=page.evaluate('assemblyData')
        (args.out/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),'utf-8')
        if args.apply:
            page.locator('#tip-direction').select_option(args.tip_direction)
            page.locator('#preview-taper').click()
            page.wait_for_function('!document.querySelector("#preview-message").hidden || !document.querySelector("#error").hidden',timeout=180000)
            assert page.locator('#error').is_hidden(),page.locator('#error').inner_text()
            assert page.evaluate('assemblyData.scene')==before['scene']
            live=page.request.get(urljoin(args.url,'/api/assemblies/'+before['id'])).json()
            assert live['scene']==before['scene'] and live['revision']==before['revision']
            page.screenshot(path=str(args.out/'preview.png'))
            page.locator('#cancel-preview').click()
            assert page.evaluate('Object.entries(assemblyData.scene.poses).every(([id,p])=>assemblyView.groups.get(id).position.toArray().every((v,i)=>v===p.position[i]))')
            page.locator('#apply-taper').click()
            page.wait_for_function('rev=>assemblyData.revision>rev || !document.querySelector("#error").hidden',arg=before['revision'],timeout=180000)
            assert page.locator('#error').is_hidden(),page.locator('#error').inner_text()
        after=page.evaluate('assemblyData')
        assert after['faces']==before['faces'],'Rigid assembly must retain all original geometry and BOM.'
        rule=after['scene']['taper_rule']
        assert rule['tip_direction']==args.tip_direction
        for fid,pose in before['scene']['poses'].items():
            if fid==rule['a'] or fid not in rule['face_ids']:
                assert after['scene']['poses'][fid]==pose,'Only the supported faces may move.'
        relation=next(p for p in after['assessment']['parallel_relations'] if p['constraint_kind']=='taper')
        assert relation['taper']['narrowing'] and relation['group_connected']
        assert after['assessment']['located_face_count']>=4
        pending=sum(not link['valid'] for link in after['assessment']['links'])
        assert after['assessment']['pending_connection_count']==pending
        if pending:assert not relation['satisfied'] and not after['assessment']['watertight']
        page.locator('#focus-group').click();page.locator('#envelopes').uncheck()
        page.wait_for_timeout(300);page.screenshot(path=str(args.out/'perspective.png'))
        page.locator('#plan-group').click();page.wait_for_timeout(300)
        direction=page.evaluate('assemblyView.camera.getWorldDirection(assemblyView.camera.position.clone()).toArray()')
        assert direction[2]<-.9999,'Plan button must look down the world Z axis.'
        page.screenshot(path=str(args.out/'plan.png'))
        page.locator('#member-label-scope').select_option('all');page.wait_for_timeout(200)
        assert page.evaluate('assemblyView.memberLabels.items.some(i=>i.group.visible)')
        page.screenshot(path=str(args.out/'plan-labels.png'))
        page.reload();page.wait_for_function('window.assemblyReady===true')
        assert page.evaluate('assemblyData.scene')==after['scene']
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert not errors and not failed and not external,(errors,failed,external)
        browser.close()
    (args.out/'after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2),'utf-8')
    summary={'url':args.url,'revision':after['revision'],'preview_did_not_save':args.apply,
             'geometry_preserved':True,'unrelated_faces_preserved':True,'root_preserved':True,
             'located_face_count':after['assessment']['located_face_count'],
             'pending_connection_count':pending,'taper':relation['taper'],
             'links':[{k:l[k] for k in ['a','b','flip','endpoint_gap_mm','angle_error_deg','valid']} for l in after['assessment']['links']],
             'reload_persists':True,'top_view':True,'page_errors':errors,'failed_requests':failed,'external_requests':external}
    (args.out/'browser-check.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(summary,ensure_ascii=True))


if __name__=='__main__':main()
