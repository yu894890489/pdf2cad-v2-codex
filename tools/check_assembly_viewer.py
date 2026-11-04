"""Verify local assembly rendering, edge connection, rigid placement and persistence."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--out',type=Path,default=Path('output/assembly-check'))
    parser.add_argument('--browser',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors=[];external=[];failed=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.browser,headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1600,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
        page.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        page.goto(args.url)
        page.wait_for_function('window.assemblyReady === true',timeout=60000)
        original=page.evaluate('({input_digest:assemblyData.input_digest,scene:assemblyData.scene})')
        stats=page.evaluate('({faces:assemblyData.faces.length,groups:assemblyView.groups.size,status:assemblyData.assessment.status,members:assemblyData.faces.reduce((n,f)=>n+f.members.length,0),parallel:assemblyData.parallel.length})')
        assert stats['faces']==stats['groups'] and stats['faces']>1
        assert page.locator('#error').is_hidden()
        page.screenshot(path=str(args.out/'all-faces.png'))
        # Actual mouse orbit, not just a state variable.
        before=page.evaluate('assemblyView.camera.quaternion.toArray()')
        box=page.locator('#assembly-canvas').bounding_box();x=box['x']+box['width']/2;y=box['y']+box['height']/2
        page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+80,y+35,steps=8);page.mouse.up()
        page.wait_for_function('q=>assemblyView.camera.quaternion.toArray().some((v,i)=>Math.abs(v-q[i])>.01)',arg=before)
        # Choose by clicking a rendered edge in the selected face.
        page.locator('#focus').click();page.locator('#mode').select_option('edge')
        point=page.evaluate('''() => {const fid=assemblyData.scene.front_face_id,face=assemblyData.faces.find(f=>f.id===fid),edge=face.edges.find(e=>e.boundary)||face.edges[0],g=assemblyView.groups.get(fid),v=g.position.clone().fromArray(edge.a).lerp(g.position.clone().fromArray(edge.b),.37);g.localToWorld(v);v.project(assemblyView.camera);const b=document.querySelector('#assembly-canvas').getBoundingClientRect();return {x:b.x+(v.x+1)*b.width/2,y:b.y+(1-v.y)*b.height/2};}''')
        page.mouse.click(point['x'],point['y'])
        assert page.locator('#edge-a').input_value()
        page.locator('#clear-edges').click()
        # Drag a visible face directly in the viewport and wait for persistence.
        page.locator('#mode').select_option('drag')
        old_pose=page.evaluate('assemblyData.scene.poses[assemblyData.scene.front_face_id]')
        revision=page.evaluate('assemblyData.revision')
        page.mouse.move(point['x'],point['y']);page.mouse.down();page.mouse.move(point['x']+45,point['y']+20,steps=10);page.mouse.up()
        page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        assert page.evaluate('assemblyData.scene.poses[assemblyData.scene.front_face_id]')!=old_pose
        # Rigid pose edit persists through page reload.
        old_x=float(page.locator('#px').input_value());revision=page.evaluate('assemblyData.revision')
        page.locator('#px').fill(str(old_x+123));page.locator('#apply-pose').click()
        page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        page.reload();page.wait_for_function('window.assemblyReady === true')
        assert abs(float(page.locator('#px').input_value())-old_x-123)<.02
        # Select different-face edges of near equal length, then inspect server residuals.
        pair=page.evaluate('''() => {const edges=assemblyData.faces.filter(f=>f.units==='mm').flatMap(f=>f.edges);let best;for(const a of edges)for(const b of edges)if(a.face_id!==b.face_id&&(!best||Math.abs(a.length-b.length)<best.d)){best={a:a.id,b:b.id,d:Math.abs(a.length-b.length)};}return best;}''')
        page.locator('#edge-a').select_option(pair['a']);page.locator('#edge-b').select_option(pair['b']);page.locator('#angle').fill('75')
        revision=page.evaluate('assemblyData.revision');page.locator('#connect').click();page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        link=page.evaluate('assemblyData.assessment.links.at(-1)')
        assert link['angle_deg']==75 and link['angle_error_deg']<1e-4
        assert abs(link['endpoint_gap_mm']-pair['d']/2)<.001
        page.screenshot(path=str(args.out/'connected-faces.png'))
        # Restore the user's original scene using the same validated import API.
        result=page.evaluate('''async original=>{const session=await(await fetch('/api/session')).json();const r=await fetch('/api/assemblies/'+assemblyData.id,{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':session.token},body:JSON.stringify({action:'import',revision:assemblyData.revision,...original})});return r.status;}''',original)
        assert result==200
        page.reload();page.wait_for_function('window.assemblyReady === true')
        page.screenshot(path=str(args.out/'restored.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        assert not errors and not external and not failed,(errors,external,failed)
        browser.close()
    report={**stats,'mouse_orbit':True,'edge_click':True,'mouse_face_drag':True,'pose_persistence':True,'manual_angle_connection':True,'original_restored':True,'mobile_layout':True,'page_errors':errors,'external_requests':external,'failed_requests':failed}
    (args.out/'browser-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
