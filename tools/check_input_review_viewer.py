"""Verify that a conflicting BOM stays visible and never becomes a model."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8772/')
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=Path('output/input-review-check'))
    parser.add_argument('--browser', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    origin = args.url.rstrip('/')
    def jobs():
        with urlopen(origin + '/api/jobs', timeout=10) as response:
            return json.load(response)['jobs']
    old = {j['id'] for j in jobs()}
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True)
        page = browser.new_page(viewport={'width':1400, 'height':1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.wait_for_function('window.studioReady === true')
        page.locator('#files').set_input_files(str(args.pdf.resolve()))
        page.locator('#upload').click()
        page.wait_for_function('document.getElementById("upload-status").textContent.includes("可在下方查看进度和结果")')
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            fresh = [j for j in jobs() if j['id'] not in old]
            if len(fresh) == 1 and fresh[0]['status'] not in {'queued', 'running'}:
                break
            page.wait_for_timeout(1000)
        assert len(fresh) == 1
        job = fresh[0]
        assert job['status'] == 'unsupported', job
        assert job['workflow']['bom_integrity']['status'] == 'blocked'
        assert any(i['code'] == 'conflicting_bom_rows' for i in job['workflow']['bom_integrity']['issues'])
        assert 'model' not in job['links'] and 'topology' not in job['links']
        page.locator('#refresh').click()
        card = page.locator(f'[data-job-id="{job["id"]}"]')
        page.wait_for_function('id=>document.querySelector(`[data-job-id="${id}"]`)?.dataset.status==="unsupported"', arg=job['id'])
        assert 'BOM 原始行需要核对' in card.inner_text()
        assert card.get_by_text('输入检查', exact=True).is_visible()
        assert card.get_by_text('BOM 原始行', exact=True).is_visible()
        assert not card.get_by_text('打开图纸与三维', exact=True).count()
        with urlopen(origin + job['links']['bomrows']) as response:
            rows = json.load(response)
        assert len(rows) >= 2 and len({r['evidence']['page'] for r in rows}) >= 2
        with urlopen(origin + job['links']['inputreport']) as response:
            report = response.read().decode('utf-8')
        assert '冲突' in report and '第 1 页' in report and '第 2 页' in report
        card.screenshot(path=str(args.out/'blocked-bom.png'))
        assert not errors, errors
        browser.close()
    (args.out/'browser-check.json').write_text(json.dumps({'job':job, 'conflict_preserved':True,
        'no_model_exposed':True, 'browser_errors':errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Conflicting PDF: original rows, page evidence, visible explanation and no model passed')


if __name__ == '__main__':
    main()
