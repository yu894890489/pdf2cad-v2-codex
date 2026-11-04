"""Apply an explicitly requested face correction and check its local viewer lifecycle.

Leaves the selected face corrected and placed. Undo/redo is exercised only for
the edit performed here; unrelated faces and native source files are preserved.
"""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import numpy as np
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--face-id',required=True)
    parser.add_argument('--root',type=Path,default=Path('runs/studio-complete'))
    parser.add_argument('--out',type=Path,default=Path('output/missing-face-correction-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors,failures,external=[],[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,
            args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1700,'height':1080})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failures.append([r.status,r.url]) if r.status>=400 else None)
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc not in {'',urlsplit(args.url).netloc} else None)
        page.goto(args.url);page.wait_for_function('window.assemblyReady===true')
        before=page.evaluate('assemblyData');fid=args.face_id
        original=next(f for f in before['faces'] if f['id']==fid)
        source=args.root/original['source_id']
        paths=[source/'source.pdf',source/'run/site/tables/bom.json']
        paths += [source/'run/site/topology'/name for name in ['members.json','geometry.json','model-preview.json','views.json']]
        def hashes():return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
        source_hashes=hashes();assert source_hashes
        (args.out/'before-browser-action.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf8')
        page.locator('#missing-face').select_option(fid)
        def action(button):
            rev=page.evaluate('assemblyData.revision');page.locator(button).click()
            page.wait_for_function('(r)=>assemblyData.revision>r&&!document.getElementById("auto").disabled',arg=rev,timeout=90000)
            return page.evaluate('assemblyData')
        applied=action('#review-missing-face');report=applied['missing_face_review']
        assert report and report['correction']['status']=='applied_candidate',report
        corrected=next(f for f in applied['faces'] if f['id']==fid)
        assert len(corrected['members'])==len(original['members'])
        assert report['correction']['max_endpoint_gap_mm']<=3.001
        assert not report['engineering_verified']
        for f in before['faces']:
            if f['id']!=fid:
                assert next(a for a in applied['faces'] if a['id']==f['id'])==f
                assert applied['scene']['poses'][f['id']]==before['scene']['poses'][f['id']]
        assert all(a['section']==b['section'] and a['bom_length_mm']==b['bom_length_mm'] for a,b in zip(original['members'],corrected['members']))
        visual=page.evaluate('''(fid)=>({
            meshes:assemblyView.pickables.filter(o=>o.userData.faceId===fid&&o.userData.memberKey).map(o=>({key:o.userData.memberKey,kind:o.userData.correctionKind,color:o.material.color.getHex(),depth:o.geometry.parameters.options.depth})),
            labels:assemblyView.memberLabels.items.filter(i=>i.faceId===fid).map(i=>i.key),
            trims:assemblyView.trimGroups.get(fid).children.map(o=>({kind:o.userData.correctionKind,points:Array.from(o.geometry.attributes.position.array)})),
            trimsOutsideModel:assemblyView.trimRoot.parent===assemblyView.scene,
            pose:assemblyView.groups.get(fid).position.toArray()
        })''',fid)
        assert visual['trimsOutsideModel'] and len(set(visual['labels']))==len(corrected['members'])
        assert np.allclose(visual['pose'],applied['scene']['poses'][fid]['position'])
        for member in corrected['members']:
            meshes=[m for m in visual['meshes'] if m['key']==fid+'/'+member['id']]
            assert abs(sum(m['depth'] for m in meshes)-member['correction']['corrected_length_mm'])<.001
            for mesh in meshes:assert mesh['color']==(0xf16b13 if mesh['kind']=='added' else 0xe6b800)
        assert any(m['kind']=='added' for m in visual['meshes'])
        page.locator('#trimmed-segments').uncheck();page.wait_for_function('!assemblyView.trimRoot.visible')
        page.locator('#trimmed-segments').check();page.wait_for_function('assemblyView.trimRoot.visible')
        page.wait_for_function('document.getElementById("missing-face-crop").complete&&document.getElementById("missing-face-crop").naturalWidth>0')
        for selector,filename in [('#missing-face-corrected','corrected.svg'),('#missing-face-source','source.svg'),('#missing-face-report','review.json')]:
            response=page.request.get(urljoin(args.url,page.locator(selector).get_attribute('href')))
            assert response.status==200;(args.out/filename).write_bytes(response.body())
        # Orbit controls must continue rotating the real corrected assembly.
        position=page.evaluate('assemblyView.camera.position.toArray()');box=page.locator('#assembly-canvas').bounding_box()
        x,y=box['x']+box['width']*.55,box['y']+box['height']*.5
        page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+80,y+35,steps=8);page.mouse.up()
        page.wait_for_function('(p)=>assemblyView.camera.position.toArray().some((v,i)=>Math.abs(v-p[i])>1)',arg=position)
        page.locator('#focus-group').click();page.locator('#envelopes').uncheck()
        page.wait_for_timeout(350);page.screenshot(path=str(args.out/'corrected-perspective.png'))
        page.locator('#focus-missing-face').click();page.wait_for_timeout(350)
        page.screenshot(path=str(args.out/'corrected-bottom.png'))
        changed=before['scene']!=applied['scene']
        if changed:
            undone=action('#undo');assert undone['faces']==before['faces'] and undone['scene']==before['scene']
            redone=action('#redo');assert redone['faces']==applied['faces'] and redone['scene']==applied['scene']
        page.reload();page.wait_for_function('window.assemblyReady===true')
        assert page.evaluate('assemblyData.faces')==applied['faces']
        assert page.evaluate('assemblyData.scene')==applied['scene']
        assert page.locator('#missing-face').input_value()==fid
        repeat=action('#review-missing-face')
        assert repeat['faces']==applied['faces'] and repeat['scene']==applied['scene']
        assert repeat['history']==applied['history']
        assert hashes()==source_hashes
        assert not errors and not failures and not external,(errors,failures,external)
        result={'revision':repeat['revision'],'corrected_face':fid,'members':len(corrected['members']),
            'connected_faces':repeat['assessment']['located_face_count'],'correction':{k:v for k,v in report['correction'].items() if k not in {'members','links','pose','outline_local'}},
            'undo_redo_tested':changed,'source_hashes':source_hashes,
            'checks':['entire face placed','solid L-section additions','removed segments excluded from model','original BOM and sources preserved','unrelated faces preserved','rotation','undo/redo' if changed else 'idempotent existing correction','reload','repeat without accumulating length edits'],
            'errors':errors,'failed_requests':failures,'external_requests':external}
        (args.out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False));browser.close()


if __name__=='__main__':main()
