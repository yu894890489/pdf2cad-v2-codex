"""Exercise real mouse dragging and persistence on an isolated copy of a project.

The supplied studio is read only. All mutations use a separate local server.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import threading
import uuid

from playwright.sync_api import sync_playwright
from tower_rebuild.studio import create_studio


def close(a, b, tolerance=1e-6):
    return len(a) == len(b) and all(abs(x-y) < tolerance for x, y in zip(a, b))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--studio-root', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--out', type=Path, default=Path('output/assembly-drag-check'))
    args = parser.parse_args()
    original = args.studio_root / 'assemblies' / args.project
    original_bytes = (original / 'project.json').read_bytes()
    record = json.loads(original_bytes)
    assert all(json.loads((args.studio_root / jid / 'job.json').read_text(encoding='utf8'))['status']
               not in {'queued', 'running'} for jid in record['job_ids'])
    isolated = args.out / ('studio-' + uuid.uuid4().hex[:8])
    isolated.mkdir(parents=True)
    for jid in record['job_ids']:
        shutil.copytree(args.studio_root / jid, isolated / jid)
    shutil.copytree(original, isolated / 'assemblies' / args.project)
    server, store = create_studio(isolated, 0, 'examples/workflow.native.json')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f'http://127.0.0.1:{server.server_port}/assemblies/{args.project}/'
    checks, errors, posts = [], [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width':1650, 'height':1040})
            page.set_default_timeout(90000)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: posts.append(r.post_data_json) if r.method=='POST' and '/api/assemblies/' in r.url else None)
            page.goto(url)
            page.wait_for_function('window.assemblyReady===true')
            assert page.locator('#mode').input_value() == 'drag'
            assert page.locator('#drag-model').get_attribute('aria-pressed') == 'true'
            baseline = page.evaluate('assemblyData')
            assert len(baseline['scene']['links']) > 0, 'Use an assembled project to verify group movement.'

            def state():
                return page.evaluate('assemblyData')

            def wait_saved(revision):
                page.wait_for_function('(r)=>assemblyData.revision>r&&!document.getElementById("mode").disabled', arg=revision)
                assert page.locator('#error').is_hidden(), page.locator('#error').inner_text()
                return state()

            def handle():
                page.locator('#focus-group').click()
                return page.evaluate('''() => {
                    const v=assemblyView, ids=v.component(assemblyData.scene.front_face_id);
                    v.root.updateMatrixWorld(true);v.camera.updateMatrixWorld(true);
                    const b=v.renderer.domElement.getBoundingClientRect();
                    for(const f of assemblyData.faces.filter(f=>ids.has(f.id))){
                        const g=v.groups.get(f.id);
                        for(const edge of f.edges)for(const ratio of [.37,.62,.2,.8]){
                            const p=g.position.clone().fromArray(edge.a).lerp(g.position.clone().fromArray(edge.b),ratio);
                            g.localToWorld(p);p.project(v.camera);
                            const point={clientX:b.x+(p.x+1)*b.width/2,clientY:b.y+(1-p.y)*b.height/2};
                            if(point.clientX<b.x+35||point.clientX>b.right-70||point.clientY<b.y+65||point.clientY>b.bottom-100)continue;
                            const fid=v.faceHit(point);
                            if(ids.has(fid))return {...point,fid,ids:[...v.component(fid)]};
                        }
                    }
                    throw Error('No visible model drag handle');
                }''')

            def begin(point):
                page.mouse.move(point['clientX'], point['clientY'])
                page.mouse.down()
                page.wait_for_function('!!assemblyView.drag')
                assert page.evaluate('assemblyView.drag.fid') == point['fid']

            def move(point):
                page.mouse.move(point['clientX']+45, point['clientY']-35, steps=10)

            def verify_translation(before, after, point, group, axis):
                fid = point['fid']
                delta = [b-a for a,b in zip(before['scene']['poses'][fid]['position'], after['scene']['poses'][fid]['position'])]
                assert sum(v*v for v in delta) > 1
                moving = set(point['ids']) if group else {fid}
                for other, old in before['scene']['poses'].items():
                    new = after['scene']['poses'][other]
                    expected = [v+(delta[i] if other in moving else 0) for i,v in enumerate(old['position'])]
                    assert close(new['position'], expected), (other, new['position'], expected)
                    assert close(new['quaternion'], old['quaternion'])
                if axis in {'x','y','z'}:
                    assert all(abs(delta[i])<1e-6 for i in range(3) if i != 'xyz'.index(axis)), delta
                if axis == 'xy':
                    assert abs(delta[2])<1e-6, delta
                assert before['faces'] == after['faces'], 'Rod / plate / hole geometry changed'
                assert before['scene']['links'] == after['scene']['links']
                assert after['history']['undo'] == before['history']['undo']+1
                checks.append({'movement':axis,'scope':'connected' if group else 'single','face_count':len(moving),'delta':delta})

            # One actual save per gesture; every linked face gets the same translation.
            for axis in ('screen','x','y','z','xy'):
                page.locator('#drag-axis').select_option(axis)
                point = handle()
                before = state()
                camera = page.evaluate('assemblyView.camera.position.toArray()')
                count = len(posts)
                begin(point)
                move(point)
                assert close(page.evaluate('assemblyView.camera.position.toArray()'), camera)
                assert page.locator('#move-connected').is_disabled()
                page.mouse.up()
                after = wait_saved(before['revision'])
                assert len(posts) == count+1
                verify_translation(before, after, point, True, axis)
                # Server roundtrip + reload restores the saved position.
                page.reload()
                page.wait_for_function('window.assemblyReady===true')
                assert state()['scene'] == after['scene']

            page.locator('#move-connected').uncheck()
            page.locator('#drag-axis').select_option('screen')
            point = handle()
            before = state()
            begin(point);move(point);page.mouse.up()
            after = wait_saved(before['revision'])
            verify_translation(before, after, point, False, 'screen')
            page.locator('#undo').click()
            undone = wait_saved(after['revision'])
            assert undone['scene'] == before['scene']
            page.locator('#redo').click()
            redone = wait_saved(undone['revision'])
            assert redone['scene'] == after['scene']
            checks.append('undo and redo restore exact saved scenes')

            # A click is selection only; Esc, lost capture and pointer cancellation
            # return all meshes to their saved positions without creating history.
            point = handle()
            before = state();count = len(posts)
            page.mouse.click(point['clientX'],point['clientY'])
            for cancel in ('escape','pointercancel','lostpointercapture','blur'):
                point=handle();begin(point);move(point)
                if cancel=='escape': page.keyboard.press('Escape')
                elif cancel=='blur': page.evaluate('window.dispatchEvent(new Event("blur"))')
                else: page.evaluate('(kind)=>assemblyView.renderer.domElement.dispatchEvent(new PointerEvent(kind,{pointerId:assemblyView.drag.pointerId}))',cancel)
                page.mouse.up()
                assert page.evaluate('!assemblyView.drag&&assemblyView.controls.enabled')
                assert page.evaluate('Object.entries(assemblyData.scene.poses).every(([id,p])=>assemblyView.groups.get(id).position.distanceTo(assemblyView.groups.get(id).position.clone().fromArray(p.position))<1e-6)')
            assert len(posts)==count and state()['revision']==before['revision']
            checks.append('selection and all cancelled gestures leave layout and history unchanged')

            # Degenerate axis direction does not lock camera controls or save.
            page.locator('#drag-axis').select_option('z')
            page.locator('#plan-group').click()
            point=page.evaluate('''()=>{
                const v=assemblyView,b=v.renderer.domElement.getBoundingClientRect();
                for(let x=.2;x<.8;x+=.05)for(let y=.2;y<.8;y+=.05){
                    const p={clientX:b.x+x*b.width,clientY:b.y+y*b.height};
                    if(v.faceHit(p))return p;
                }
                throw Error('No top-view hit');
            }''')
            page.mouse.click(point['clientX'],point['clientY'])
            assert page.evaluate('!assemblyView.drag&&assemblyView.controls.enabled')
            assert '视线' in page.locator('#hint').inner_text()
            assert len(posts)==count
            checks.append('near-parallel axis gives guidance without locking navigation')

            # Save failure must restore both the meshes and the position fields.
            page.locator('#drag-axis').select_option('screen')
            point=handle();before=state()
            def reject_save(route):
                if route.request.method=='POST': route.fulfill(status=500,json={'error':'测试：保存失败'})
                else: route.continue_()
            page.route('**/api/assemblies/'+args.project,reject_save)
            begin(point);move(point);page.mouse.up()
            page.wait_for_function('!document.getElementById("error").hidden&&!document.getElementById("mode").disabled')
            assert state()['scene']==before['scene']
            assert close([float(page.locator('#p'+a).input_value()) for a in 'xyz'],before['scene']['poses'][point['fid']]['position'],.006)
            assert page.evaluate('Object.entries(assemblyData.scene.poses).every(([id,p])=>assemblyView.groups.get(id).position.distanceTo(assemblyView.groups.get(id).position.clone().fromArray(p.position))<1e-6)')
            page.unroute('**/api/assemblies/'+args.project,reject_save)
            checks.append('failed save restores meshes and XYZ fields')
            page.reload();page.wait_for_function('window.assemblyReady===true')
            page.screenshot(path=str(args.out/'drag-controls.png'))
            # Orbit mode is still available and cannot mutate model placement.
            page.locator('#mode').select_option('orbit')
            point=handle();before=state();count=len(posts)
            camera=page.evaluate('assemblyView.camera.position.toArray()')
            page.mouse.move(point['clientX'],point['clientY']);page.mouse.down();move(point);page.mouse.up()
            assert not close(page.evaluate('assemblyView.camera.position.toArray()'),camera)
            assert state()['scene']==before['scene'] and len(posts)==count
            page.locator('#drag-model').click()
            assert page.locator('#mode').input_value()=='drag'
            checks.append('orbit mode changes camera only; button returns to drag mode')
            assert not errors,errors
            browser.close()
        assert (original/'project.json').read_bytes()==original_bytes, 'Original project was modified'
        result={'checks':checks,'errors':errors,'original_project_unchanged':True,
                'original_sha256':hashlib.sha256(original_bytes).hexdigest(),'isolated_root':str(isolated)}
        (args.out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        server.shutdown();server.server_close();store.close();worker.join()


if __name__=='__main__':
    main()
