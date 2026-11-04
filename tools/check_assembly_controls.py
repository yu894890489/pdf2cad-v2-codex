"""Verify selection persistence and the shared-member auto action in a real viewer.

This applies the user's automatic placement request to the selected local project.
It does not rearrange or remove any existing connections as test setup.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--face-id',required=True)
    parser.add_argument('--out',type=Path,default=Path('output/assembly-controls-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    errors,failures,external=[],[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,
            args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1600,'height':1050})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failures.append([r.status,r.url]) if r.status>=400 else None)
        page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc not in {'',urlsplit(args.url).netloc} else None)
        page.goto(args.url);page.wait_for_function('window.assemblyReady===true')
        before=page.evaluate('assemblyData')
        assert page.locator('#missing-face option').count()>0
        assert any(o['face_id']==args.face_id for o in before['missing_face_options'])
        page.locator('#missing-face').select_option(args.face_id)
        page.reload();page.wait_for_function('window.assemblyReady===true')
        assert page.locator('#missing-face').input_value()==args.face_id
        count=page.locator('#missing-face option').count()
        (args.out/'before-fixed-action.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf8')
        page.locator('#auto').click()
        assert page.locator('#auto').is_disabled()
        assert '正在' in page.locator('#auto').inner_text()
        page.wait_for_function('(r)=>assemblyData.revision>r&&!document.getElementById("auto").disabled',arg=before['revision'],timeout=180000)
        applied=page.evaluate('assemblyData')
        assert applied['auto_attempt']['found'] or (before['scene'].get('face_corrections') and not applied['auto_attempt']['changed']),applied['auto_attempt']
        assert page.locator('#auto-status').is_visible()
        assert applied['auto_attempt']['message']==page.locator('#auto-status').inner_text()
        assert applied['faces']==before['faces']
        root=before['scene']['front_face_id']
        assert applied['scene']['poses'][root]==before['scene']['poses'][root]
        if applied['scene'].get('taper_rule'):
            fitted=set(applied['scene']['taper_rule']['face_ids'])
            for fid,pose in before['scene']['poses'].items():
                if fid not in fitted:assert applied['scene']['poses'][fid]==pose
        assert page.locator('#missing-face option').count()==count
        assert page.locator('#missing-face').input_value()==args.face_id
        assert page.locator('#review-missing-face').evaluate('(button)=>!button.disabled')
        page.locator('#review-missing-face').click()
        page.wait_for_function('(fid)=>assemblyData.missing_face_review?.target_face_id===fid&&!document.getElementById("auto").disabled',arg=args.face_id)
        page.wait_for_function('document.getElementById("missing-face-crop").complete&&document.getElementById("missing-face-crop").naturalWidth>0')
        reviewed=page.evaluate('assemblyData')
        # Repeating automatic placement must not fill undo with identical scenes.
        page.locator('#auto').click()
        page.wait_for_function('(r)=>assemblyData.revision>r&&!document.getElementById("auto").disabled',arg=reviewed['revision'],timeout=180000)
        repeated=page.evaluate('assemblyData')
        assert not repeated['auto_attempt']['changed']
        assert repeated['history']==reviewed['history']
        assert repeated['scene']==reviewed['scene']
        assert repeated['missing_face_review']==reviewed['missing_face_review']
        page.reload();page.wait_for_function('window.assemblyReady===true')
        page.wait_for_function('document.getElementById("missing-face-crop").complete&&document.getElementById("missing-face-crop").naturalWidth>0')
        assert page.locator('#missing-face').input_value()==args.face_id
        assert page.locator('#auto-status').inner_text()==repeated['auto_attempt']['message']
        assert page.locator('#review-missing-face').evaluate('(button)=>!button.disabled')
        page.locator('#focus-missing-face').click();page.locator('#missing-face-panel').scroll_into_view_if_needed()
        page.screenshot(path=str(args.out/'selection-after-refresh.png'))
        page.locator('#auto').scroll_into_view_if_needed();page.screenshot(path=str(args.out/'auto-result.png'))
        assert not errors and not failures and not external,(errors,failures,external)
        result={'revision':repeated['revision'],'option_count':count,'selected_face_id':args.face_id,
                'connected_faces':repeated['assessment']['located_face_count'],'auto_result':repeated['auto_attempt'],
                'checks':['all structural faces visible','selection persists across refresh','auto running feedback','partial placement succeeds','geometry and unrelated poses preserved','no duplicate undo entries','review survives unchanged auto','result survives refresh'],
                'errors':errors,'failed_requests':failures,'external_requests':external}
        (args.out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False));browser.close()


if __name__=='__main__':main()
