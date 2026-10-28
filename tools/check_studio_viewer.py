"""Upload real PDFs through the local portal and verify fresh workflow results."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8770/')
    parser.add_argument('--pdf', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, default=Path('output/studio-check'))
    parser.add_argument('--browser', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    errors, external, result = [], [], {}
    origin = args.url.rstrip('/')

    def jobs():
        with urlopen(origin + '/api/jobs', timeout=10) as response:
            return json.load(response)['jobs']

    previous = {j['id'] for j in jobs()}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True,
                                   args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width':1440, 'height':1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: external.append(r.url) if urlsplit(r.url).netloc != urlsplit(origin).netloc else None)
        page.goto(args.url)
        page.wait_for_function('window.studioReady === true')
        page.locator('#files').set_input_files([str(path.resolve()) for path in args.pdf])
        assert page.locator('#selected-files li').count() == len(args.pdf)
        page.locator('#upload').click()
        page.wait_for_function('document.getElementById("upload-status").textContent.includes("可在下方查看进度和结果")', timeout=120000)
        pending = [j for j in jobs() if j['id'] not in previous]
        assert len(pending) == len(args.pdf)
        result['job_ids'] = [j['id'] for j in pending]
        print('Uploaded fresh jobs: ' + ', '.join(result['job_ids']), flush=True)
        seen, deadline = None, time.monotonic() + 1200
        while time.monotonic() < deadline:
            pending = [j for j in jobs() if j['id'] in result['job_ids']]
            status = [(j['id'][:8], j['status'], (j.get('workflow') or {}).get('current_stage')) for j in pending]
            if status != seen:
                print(status, flush=True)
                seen = status
            if all(j['status'] not in {'queued', 'running'} for j in pending):
                break
            page.wait_for_timeout(1000)
        assert all(j['status'] == 'needs_review' for j in pending), pending
        for j in pending:
            assert all(s['status'] == 'completed' or (s['id'] in {'vision_review', 'ocr'} and s['status'] == 'skipped') for s in j['workflow']['stages'])
            assert any(s['id'] == 'model_audit' for s in j['workflow']['stages'])
            assert any(s['id'] == 'final_check' and s['status'] == 'completed' for s in j['workflow']['stages'])
            with urlopen(origin + f'/results/{j["id"]}/model-audit/report.json', timeout=10) as response:
                comparison = json.load(response)
                assert comparison['stage'] == 'model_dimension_audit'
                assert comparison['source_sha256'] == j['workflow']['source_sha256']
            assert not j['workflow']['engineering_accuracy_verified']
            assert j['workflow']['bom_integrity']['status'] == 'passed'
            for name in ['tables', 'topology', 'audit', 'final', 'bom', 'model', 'report', 'bomrows', 'bomcheck', 'inputreport']:
                with urlopen(origin + j['links'][name], timeout=10) as response:
                    assert response.status == 200
        page.locator('#refresh').click()
        page.wait_for_function('ids=>ids.every(id=>document.querySelector(`[data-job-id="${id}"]`)?.dataset.status==="needs_review")', arg=result['job_ids'])
        page.screenshot(path=str(args.out/'studio-desktop.png'), full_page=True)
        page.reload()
        page.wait_for_function('window.studioReady === true')
        assert all(page.locator(f'[data-job-id="{jid}"]').count() == 1 for jid in result['job_ids'])
        page.set_viewport_size({'width':390, 'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(args.out/'studio-mobile.png'), full_page=True)
        page.set_viewport_size({'width':1440, 'height':1100})
        for j in pending:
            page.goto(origin + j['links']['final'])
            page.wait_for_function('window.finalCheckReady === true')
            assert page.locator('#checks tr').count() >= 10
            assert '尚未确认' in page.locator('#summary').inner_text()
            page.screenshot(path=str(args.out/f'final-{j["id"][:8]}.png'))
            page.set_viewport_size({'width':390, 'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.set_viewport_size({'width':1440, 'height':1100})
            page.goto(origin + j['links']['topology'])
            page.wait_for_function('window.preflightReady === true')
            page.locator('#model-toggle').click()
            page.wait_for_function('window.modelPreview?.visible === true')
            assert page.locator('#model-message.failure').count() == 0
            assert page.evaluate('window.modelPreview.stats.angle_meshes') > 0
            before = page.evaluate('window.modelPreview.camera.quaternion.toArray()')
            box = page.locator('#model-canvas').bounding_box()
            page.mouse.move(box['x']+box['width']/2, box['y']+box['height']/2)
            page.mouse.down()
            page.mouse.move(box['x']+box['width']/2+80, box['y']+box['height']/2+40, steps=10)
            page.mouse.up()
            page.wait_for_function('q=>window.modelPreview.camera.quaternion.toArray().some((v,i)=>Math.abs(v-q[i])>.01)', arg=before)
            page.screenshot(path=str(args.out/f'model-{j["id"][:8]}.png'), full_page=True)
        assert not errors and not external, (errors, external)
        result.update(jobs=pending, browser_errors=errors, external_requests=external,
                      fresh_pipeline_verified=True, history_after_reload=True,
                      mobile_layout=True, rotatable_models=True, engineering_accuracy_verified=False)
        browser.close()
    (args.out/'studio-browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(pending)} PDFs: fresh upload, workflow stages including final check, reports, persistent history and actual 3D rotation passed', flush=True)


if __name__ == '__main__':
    main()
