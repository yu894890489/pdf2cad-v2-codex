"""Exercise every local per-view 3D overlay, actual rotation and failure UI."""
import argparse
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from tower_rebuild.server import LocalHTTPServer


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--url')
    parser.add_argument('--browser',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args=parser.parse_args();server=None
    model=json.loads((args.out/'site/topology/model-preview.json').read_text(encoding='utf-8'))
    if not args.url:
        server=LocalHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str((args.out/'site').resolve())))
        threading.Thread(target=server.serve_forever,daemon=True).start();args.url=f'http://127.0.0.1:{server.server_port}/topology/'
    errors=[];failed=[];external=[];checked=[];alignments=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=args.browser,headless=True,
                args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1560,'height':1150})
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('requestfailed',lambda r:failed.append(r.url))
            page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
            page.goto(args.url,wait_until='networkidle');page.wait_for_function('window.preflightReady === true')
            assert page.locator('#model-overlay').is_hidden()
            page.locator('#model-toggle').click();page.wait_for_function('window.modelPreview?.visible === true')
            assert page.locator('#model-message.failure').count()==0
            for view in model['views']:
                page.locator(f'.view-button[data-id="{view["view_id"]}"]').click()
                page.wait_for_function('id=>window.modelPreview?.stats.view_id===id',arg=view['view_id'])
                stats=page.evaluate('window.modelPreview.stats')
                assert stats['angle_meshes']==view['counts']['angle_models']
                assert stats['plate_meshes']==view['counts']['plate_polygons']
                assert stats['hole_markers']==view['counts']['hole_projections']
                assert stats['drilled_plate_holes']==sum(len(p['drill_holes']) for p in view['plates'])
                if not stats['angle_meshes'] and not stats['plate_meshes']:
                    assert '尚无可生成' in page.locator('#model-message').inner_text()
                else:
                    page.locator('#model-align').click()
                    deviation=page.evaluate('''() => {
                        const m=window.modelPreview,t=transform(),b=m.model.bbox_pt;
                        const p=[b[0]+(b[2]-b[0])*.2,b[1]+(b[3]-b[1])*.6];
                        const q=new m.camera.position.constructor(p[0]-m.origin[0],m.origin[1]-p[1],0).project(m.camera);
                        const rect=m.renderer.domElement.getBoundingClientRect();
                        return Math.hypot((q.x+1)/2*rect.width-(p[0]*t.k+t.ox),(1-q.y)/2*rect.height-(p[1]*t.k+t.oy));
                    }''')
                    assert deviation<1,deviation;alignments.append({'view_id':view['view_id'],'pixel_error':deviation})
                    before=page.evaluate('window.modelPreview.camera.quaternion.toArray()')
                    paper=page.evaluate('document.getElementById("drawing").toDataURL()')
                    box=page.locator('#model-canvas').bounding_box();x=box['x']+box['width']*.5;y=box['y']+box['height']*.5
                    page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+70,y+35,steps=8);page.mouse.up()
                    page.wait_for_function('q=>window.modelPreview.camera.quaternion.toArray().some((x,i)=>Math.abs(x-q[i])>.01)',arg=before)
                    assert paper==page.evaluate('document.getElementById("drawing").toDataURL()'),'rotating model moved drawing'
                    before=page.evaluate('window.modelPreview.camera.zoom');page.mouse.wheel(0,-120)
                    page.wait_for_function('z=>window.modelPreview.camera.zoom>z',arg=before)
                for kind in ('members','plates','holes'):
                    page.locator(f'#model-{kind}').uncheck()
                    assert not page.evaluate(f'window.modelPreview.groups.{kind}.visible')
                    page.locator(f'#model-{kind}').check()
                if view['view_id']==model['views'][3]['view_id']:
                    page.locator('#model-oblique').click();page.locator('.canvas-wrap').screenshot(path=str(args.out/'model-preview-detail.png'))
                    page.screenshot(path=str(args.out/'model-preview-browser.png'),full_page=True)
                if view['counts']['explicit_hole_diameters'] and view['counts']['plate_polygons']:
                    page.locator('#model-align').click()
                    # Extrusions must actually contain holes, not just ring overlays.
                    shapes=page.evaluate('window.modelPreview.groups.plates.children.map(m=>m.geometry.parameters.shapes.holes.length)')
                    assert sum(shapes)==stats['drilled_plate_holes']
                    page.locator('#model-oblique').click();page.locator('.canvas-wrap').screenshot(path=str(args.out/f'model-preview-{view["view_id"]}.png'))
                checked.append(stats)
            # L-section geometry must have six section vertices and a missing quadrant.
            section=page.evaluate('''() => {
                const v=state.modelData.views.find(v=>v.members.length);window.modelPreview.setView(v,transform());
                const points=window.modelPreview.groups.members.children[0].geometry.parameters.shapes.getPoints();
                let area=0;for(let i=0;i<points.length;i++){const a=points[i],b=points[(i+1)%points.length];area+=a.x*b.y-a.y*b.x;}
                const m=v.members[0],s=v.scale.mm_per_pt,[a,b]=m.section.legs_mm,t=m.section.thickness_mm;
                return {actual:Math.abs(area/2),expected:(a*t+b*t-t*t)/(s*s)};
            }''')
            assert abs(section['actual']-section['expected'])<1e-6,section
            page.locator('#model-toggle').click();assert page.locator('#model-overlay').is_hidden()
            page.locator('#model-toggle').click();assert page.locator('#model-overlay').is_visible()
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#model-align').click()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            page.locator('.canvas-wrap').screenshot(path=str(args.out/'model-preview-mobile.png'))
            assert not errors and not failed and not external,(errors,failed,external)
            # Invalid model data has a retryable failure, while the drawing remains usable.
            page.route('**/topology/model-preview.json',lambda route:route.fulfill(status=200,body='broken'))
            page.reload();page.wait_for_function('window.topologyReady === true');page.locator('#model-toggle').click()
            page.wait_for_selector('#model-message.failure');assert '三维模型未加载' in page.locator('#model-message').inner_text()
            assert page.locator('#drawing').is_visible()
            page.unroute('**/topology/model-preview.json');page.locator('#model-toggle').click()
            page.wait_for_function('window.modelPreview?.visible === true');browser.close()
            # A host without WebGL must get an explicit failure, not a loading spinner.
            browser=p.chromium.launch(executable_path=args.browser,headless=True,args=['--disable-webgl','--disable-gpu'])
            page=browser.new_page();page.goto(args.url);page.wait_for_function('window.preflightReady === true')
            page.locator('#model-toggle').click();page.wait_for_selector('#model-message.failure')
            assert page.locator('#drawing').is_visible();browser.close()
    finally:
        if server:server.shutdown();server.server_close()
    result={'views':checked,'alignment_checks':alignments,'angle_section_area_check':section,
            'rotation_and_zoom_verified':True,'drawing_unchanged_during_rotation':True,'local_only':not external,
            'javascript_errors':errors,'failed_requests':failed,'invalid_model_retry':True,'webgl_failure_visible':True,
            'engineering_accuracy_verified':False}
    (args.out/'model-preview-browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'{len(checked)} views: real 3D rotation, zoom, overlays, alignment, L profiles, plate holes and failure states passed')


if __name__=='__main__':main()
