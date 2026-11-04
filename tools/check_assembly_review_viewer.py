"""Exercise conflict evidence, non-saving preview, and persisted undo/redo."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--mark',required=True)
    parser.add_argument('--angle',type=float,default=90)
    parser.add_argument('--same-direction',action='store_true')
    parser.add_argument('--keep-connection',action='store_true')
    parser.add_argument('--out',type=Path,default=Path('output/assembly-review-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors=[];external=[];failed=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1600,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
        page.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        page.goto(args.url);page.wait_for_function('window.assemblyReady')
        original=page.evaluate('JSON.parse(JSON.stringify(assemblyData.scene))')
        revision=page.evaluate('assemblyData.revision')
        match=page.evaluate('''mark=>{const edges=new Map(assemblyData.faces.flatMap(f=>f.edges.map(e=>[e.id,e])));return assemblyData.matches.find(m=>m.shared_member_candidate&&edges.get(m.a).mark===mark);}''',args.mark)
        assert match
        page.locator(f'[data-match-id="{match["id"]}"]').click()
        page.wait_for_function('!document.querySelector("#preview-message").hidden')
        assert page.evaluate('assemblyData.revision')==revision
        page.locator('#cancel-preview').click()
        assert page.evaluate('assemblyData.scene')==original
        page.locator('#angle').fill(str(args.angle));page.locator('#flip').set_checked(not args.same_direction)
        page.locator('#preview-connect').click();page.wait_for_function('!document.querySelector("#preview-message").hidden')
        persisted=page.evaluate('async()=>await(await fetch("/api/assemblies/"+assemblyData.id)).json()')
        assert persisted['revision']==revision and persisted['scene']==original
        assert page.locator('#bridge-conflicts').inner_text()
        page.screenshot(path=str(args.out/'connection-preview.png'))
        page.locator('#connect').click();page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        connected=page.evaluate('JSON.parse(JSON.stringify(assemblyData.scene))')
        residual=page.evaluate('assemblyData.assessment.links.at(-1)')
        assert residual['valid'] and residual['angle_deg']==args.angle
        revision=page.evaluate('assemblyData.revision');page.locator('#undo').click();page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        assert page.evaluate('assemblyData.scene')==original
        revision=page.evaluate('assemblyData.revision');page.locator('#redo').click();page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
        assert page.evaluate('assemblyData.scene')==connected
        page.reload();page.wait_for_function('window.assemblyReady');assert page.evaluate('assemblyData.scene')==connected
        page.locator('#focus-group').click();page.screenshot(path=str(args.out/'partial-connection.png'))
        review=page.evaluate('assemblyData.review')
        page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        if not args.keep_connection:
            revision=page.evaluate('assemblyData.revision');page.locator('#undo').click();page.wait_for_function('r=>assemblyData.revision>r',arg=revision)
            assert page.evaluate('assemblyData.scene')==original
        assert not errors and not failed and not external,(errors,failed,external)
        browser.close()
    report={'preview_does_not_save':True,'undo_redo_persist':True,'partial_connection_kept':args.keep_connection,
            'mark':args.mark,'residual':residual,'parallel_bridges':review['parallel_bridges'],
            'length_conflict_count':len(review['length_conflicts']),'axis_overlap_count':len(review['overlapping_axes']),
            'page_errors':errors,'failed_requests':failed,'external_requests':external,'engineering_verified':False}
    (args.out/'browser-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'parallel_bridges','residual'}},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
