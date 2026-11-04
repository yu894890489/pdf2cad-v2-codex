"""Check 3D member placards, physical occlusion, picking and unchanged geometry."""
import argparse
import io
import json
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import sync_playwright

FIND_LABEL = """async kind => {
  const v=kind==='assembly'?assemblyView:modelPreview;
  const THREE=await import(kind==='assembly'?'/assembly-assets/three.module.js':'./vendor/three.module.js');
  const ray=new THREE.Raycaster(),box=v.renderer.domElement.getBoundingClientRect();
  const occluders=v.pickables.filter(o=>!o.userData.envelope);
  for(const item of v.memberLabels.items){
    if(!item.group.visible||!v.memberLabels.root.visible)continue;
    const p=item.body.getWorldPosition(new THREE.Vector3()).project(v.camera);
    if(Math.abs(p.x)>.9||Math.abs(p.y)>.9||p.z<-1||p.z>1)continue;
    ray.setFromCamera(new THREE.Vector2(p.x,p.y),v.camera);
    if(v.memberLabels.pick(ray,occluders)?.key===item.key)
      return {key:item.key,text:item.text,id:item.record.id,x:box.x+(p.x+1)*box.width/2,y:box.y+(1-p.y)*box.height/2};
  }return null;
}"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assembly-url',action='append',default=[])
    parser.add_argument('--topology-url',action='append',default=[])
    parser.add_argument('--out',type=Path,default=Path('output/member-placard-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors,failed,external,checks=[],[],[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1600,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        allowed={urlsplit(u).netloc for u in args.assembly_url+args.topology_url}
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc not in allowed else None)
        for index,(kind,url) in enumerate([('assembly',u) for u in args.assembly_url]+[('topology',u) for u in args.topology_url]):
            page.goto(url)
            if kind=='assembly':
                page.wait_for_function('window.assemblyReady===true')
                original=page.evaluate('({scene:assemblyData.scene,revision:assemblyData.revision})')
                expected=page.evaluate('assemblyData.faces.flatMap(f=>f.members.map(m=>({face:f.id,id:m.id,text:(m.side||"")+m.mark})))')
                page.locator('#focus').click()
                # Choose a face-on view for legible screenshot and picking.
                page.evaluate("""() => {
                  const v=assemblyView,f=assemblyData.scene.front_face_id,g=v.groups.get(f);
                  const center=v.controls.target.clone(),d=v.camera.position.distanceTo(center);
                  const normal=center.clone().set(0,0,1).applyQuaternion(g.quaternion);
                  v.camera.position.copy(center).addScaledVector(normal,d);v.controls.update();
                }""")
            else:
                page.wait_for_function('window.preflightReady===true')
                page.locator('#model-toggle').click();page.wait_for_function('window.modelPreview?.visible===true')
                original=page.evaluate('modelPreview.measureGeometry()')
                expected=page.evaluate('modelPreview.groups.members.children.map(m=>({face:modelPreview.model.view_id,id:m.userData.record.id,text:(m.userData.record.side||"")+m.userData.record.mark}))')
            page.wait_for_timeout(200)
            view='assemblyView' if kind=='assembly' else 'modelPreview'
            observed=page.evaluate(f'{view}.memberLabels.items.map(i=>({{face:i.faceId,id:i.record.id,text:i.text}}))')
            assert observed==expected
            assert page.locator('.member-label-layer').count()==0,'Screen overlay annotations must be gone.'
            assert page.evaluate(f'{view}.memberLabels.items.every(i=>i.body.isMesh&&i.body.material.depthTest&&i.body.material.depthWrite&&i.group.children.filter(o=>o.isMesh).length===3)')
            item=page.evaluate(FIND_LABEL,kind)
            assert item,'At least one visible placard must be pickable.'
            page.mouse.click(item['x'],item['y'])
            if kind=='assembly':
                assert page.locator('#member-title').inner_text()=='件号 '+item['text']
                assert page.evaluate('assemblyView.pickables.filter(o=>o.userData.memberKey&&o.material.color.getHex()===0xf58216).map(o=>o.userData.memberKey)')==[item['key']]
            else:
                assert json.loads(page.locator('#selected').inner_text())['id']==item['id']
            # A real occluder must cover the label in WebGL and prevent label picking.
            occlusion=page.evaluate("""async ({kind,key}) => {
              const v=kind==='assembly'?assemblyView:modelPreview;
              const THREE=await import(kind==='assembly'?'/assembly-assets/three.module.js':'./vendor/three.module.js');
              const item=v.memberLabels.items.find(i=>i.key===key),center=item.body.getWorldPosition(new THREE.Vector3());
              const scale=item.group.getWorldScale(new THREE.Vector3()),size=Math.max(item.width,item.height)*scale.x*2;
              const blocker=new THREE.Mesh(new THREE.PlaneGeometry(size,size),new THREE.MeshBasicMaterial({color:0xff00ff,side:THREE.DoubleSide}));
              blocker.position.copy(center).lerp(v.camera.position,.1);blocker.quaternion.copy(v.camera.quaternion);v.scene.add(blocker);
              window.placardTestBlocker=blocker;v.scene.updateMatrixWorld(true);
              const ray=new THREE.Raycaster(),p=center.project(v.camera);ray.setFromCamera(new THREE.Vector2(p.x,p.y),v.camera);
              v.renderer.render(v.scene,v.camera);
              return v.memberLabels.pick(ray,[...v.pickables.filter(o=>!o.userData.envelope),blocker])===null;
            }""",{'kind':kind,'key':item['key']})
            assert occlusion
            pixel=Image.open(io.BytesIO(page.screenshot())).convert('RGB').getpixel((round(item['x']),round(item['y'])))
            assert pixel[0]>240 and pixel[1]<15 and pixel[2]>240,('Label must not draw over the occluder',pixel)
            page.evaluate(f'{view}.scene.remove(placardTestBlocker);placardTestBlocker.geometry.dispose();placardTestBlocker.material.dispose();delete window.placardTestBlocker')
            # Zoom changes projected plaque size; there is no fixed pixel sizing.
            zoom=page.evaluate("""({kind,key}) => {
              const v=kind==='assembly'?assemblyView:modelPreview,item=v.memberLabels.items.find(i=>i.key===key);
              const w=v.renderer.domElement.clientWidth,h=v.renderer.domElement.clientHeight;
              const measure=()=>{const a=item.body.localToWorld(item.point.clone().set(-item.width/2,0,0)).project(v.camera),b=item.body.localToWorld(item.point.clone().set(item.width/2,0,0)).project(v.camera);return Math.hypot((a.x-b.x)*w/2,(a.y-b.y)*h/2);};
              const before=measure(),position=v.camera.position.clone(),oldZoom=v.camera.zoom,matrix=item.group.matrix.toArray();
              if(v.camera.isPerspectiveCamera)v.camera.position.sub(v.controls.target).multiplyScalar(2).add(v.controls.target);else v.camera.zoom*=.5;
              v.camera.updateProjectionMatrix();v.camera.updateMatrixWorld();const after=measure();
              v.camera.position.copy(position);v.camera.zoom=oldZoom;v.camera.updateProjectionMatrix();v.controls.update();
              return {before,after,worldMatrixUnchanged:matrix.every((x,i)=>x===item.group.matrix.elements[i])};
            }""",{'kind':kind,'key':item['key']})
            assert zoom['after']<zoom['before']*.6 and zoom['worldMatrixUnchanged']
            toggle='#member-labels' if kind=='assembly' else '#model-labels'
            page.locator(toggle).uncheck();page.wait_for_function(f'!{view}.memberLabels.root.visible')
            page.locator(toggle).check();page.wait_for_function(f'{view}.memberLabels.root.visible')
            if kind=='assembly':
                page.locator('#member-label-scope').select_option('all')
                page.wait_for_function('assemblyView.memberLabels.items.every(i=>i.group.visible)')
                page.locator('#member-label-scope').select_option('selected')
                page.locator('#member-label-size').fill('1.4')
                page.wait_for_timeout(100)
                page.locator('#member-label-size').fill('1')
                page.locator('#focus-group').click()
                assert page.evaluate('({scene:assemblyData.scene,revision:assemblyData.revision})')==original
            else:
                page.locator('#model-members').uncheck()
                page.wait_for_function('modelPreview.memberLabels.items.every(i=>!i.group.visible)')
                page.locator('#model-members').check()
                page.locator('#model-oblique').click()
                assert page.evaluate('modelPreview.measureGeometry()')==original
            page.wait_for_timeout(200);page.screenshot(path=str(args.out/f'{kind}-{index}-placards.png'))
            if kind=='assembly':
                page.locator('#member-label-scope').select_option('all');page.locator('#plan-group').click()
                page.wait_for_timeout(200);page.screenshot(path=str(args.out/f'{kind}-{index}-plan.png'))
                page.reload();page.wait_for_function('window.assemblyReady===true')
                assert page.evaluate('({scene:assemblyData.scene,revision:assemblyData.revision})')==original
            checks.append({'kind':kind,'url':url,'placards':len(expected),'unique_observation_selection':True,
                           'real_occlusion':True,'occluder_pixel_rgb':pixel,'zoom':zoom,'no_dom_labels':True,
                           'geometry_and_layout_unchanged':True})
        assert not errors and not failed and not external,(errors,failed,external)
        browser.close()
    result={'checks':checks,'page_errors':errors,'failed_requests':failed,'external_requests':external}
    (args.out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
