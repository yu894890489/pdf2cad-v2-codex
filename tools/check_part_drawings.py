"""Read-only browser/download and original-model checks for a CAD export."""
import argparse
import io
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Assembly URL')
    parser.add_argument('--out', type=Path, default=Path('output/part-drawings-check'))
    parser.add_argument('--allow-running', action='store_true')
    args = parser.parse_args();args.out.mkdir(parents=True, exist_ok=True)
    split = urlsplit(args.url);origin = f'{split.scheme}://{split.netloc}'
    aid = split.path.strip('/').split('/')[1]
    fetch = lambda url: json.load(urlopen(url, timeout=60))
    snapshot = fetch(origin+f'/api/assemblies/{aid}')
    status = fetch(origin+f'/api/assemblies/{aid}/part-drawings')
    prefix = origin+f'/assemblies/{aid}/part-drawings/'
    result = prefix+status['export_id']+'/'
    report = fetch(result+'report.json')
    if not args.allow_running:assert status['status'] == 'complete', status
    errors, external, failed = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        page.on('pageerror', lambda e:errors.append(str(e)))
        page.on('request', lambda r:external.append(r.url) if urlsplit(r.url).netloc != split.netloc else None)
        page.on('response', lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        page.goto(prefix)
        page.wait_for_function("document.querySelector('#status').textContent.includes('当前模型修订')", timeout=60000)
        print(page.locator('#status').inner_text())
        page.screenshot(path=str(args.out/'page.png'),full_page=True)
        if status['status'] == 'complete':
            page.locator('#result').wait_for(state='visible')
            assert page.locator('#error').is_hidden()
            with page.expect_download() as event:page.locator('#download').click()
            event.value.save_as(args.out/'parts-dwg.zip')
            page.goto(result+'index.html')
            assert page.locator('table tr').count() == len(report['parts'])+1
            with page.expect_download() as event:page.locator('a[download]').filter(has_text='DWG').nth(1).click()
            assert Path(event.value.path()).read_bytes()[:6] == b'AC1032'
        for kind in ('angle','plate','plate-holes'):
            part = next((p for p in report['parts'] if p['kind']==('plate' if kind=='plate-holes' else kind)
                         and p.get('svg') and (kind!='plate-holes' or p['geometry'].get('holes'))), None)
            if part:
                page.goto(result+part['svg'])
                page.screenshot(path=str(args.out/(kind+'-preview.png')))
        page.set_viewport_size({'width':430,'height':850})
        page.goto(prefix)
        page.wait_for_function("document.querySelector('#status').textContent.includes('当前模型修订')", timeout=60000)
        page.screenshot(path=str(args.out/'mobile.png'),full_page=True)
        assert not errors and not external and not failed, (errors, external, failed)
        browser.close()
    if status['status'] != 'complete':return
    with zipfile.ZipFile(args.out/'parts-dwg.zip') as archive:
        assert archive.testzip() is None
        dwgs = {n for n in archive.namelist() if n.endswith('.dwg')}
        assert dwgs == {p['dwg'] for p in report['parts']}
        assert all(archive.read(n)[:6]==b'AC1032' for n in dwgs)
    members = {(f['id'],m['id']):m for f in snapshot['faces'] for m in f['members']}
    verified = 0
    for part in report['parts']:
        assert part['verification']['roundtrip_geometry_verified']
        assert part['verification']['annotations_verified']
        if part['kind'] != 'angle':continue
        for observation in part['observations']:
            member = members[observation['face_id'],observation['component_id']]
            assert math.isclose(part['geometry']['length_mm'],math.dist(member['a'],member['b']),abs_tol=1e-6)
            verified += 1
    after = fetch(origin+f'/api/assemblies/{aid}')
    assert snapshot['scene']==after['scene'] and snapshot['faces']==after['faces']
    output = {'url': prefix, 'counts': report['counts'], 'original_member_lengths_verified':verified,
              'zip_crc_verified':True, 'scene_unchanged':True, 'browser_errors':errors,'external_requests':external,'failed_requests':failed}
    (args.out/'browser-check.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(output,ensure_ascii=False))


if __name__ == '__main__':main()
