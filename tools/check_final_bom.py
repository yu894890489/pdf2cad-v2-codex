"""Read-only browser and independent length checks for a saved assembly BOM."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Live assembly URL')
    parser.add_argument('--out', type=Path, default=Path('output/final-bom-check'))
    args = parser.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    base = urlsplit(args.url); origin = f'{base.scheme}://{base.netloc}'
    aid = base.path.strip('/').split('/')[1]
    fetch = lambda url: json.load(urlopen(url, timeout=60))
    before = fetch(origin + '/api/assemblies/' + aid)
    url = args.url.rstrip('/') + '/final-bom/'
    report = fetch(url + 'final-bom.json')
    members = {(f['id'], m['id']): (f, m) for f in before['faces'] for m in f['members']}
    verified = 0
    for row in report['rows']:
        for item in row['observations']:
            if item['kind'] != 'angle': continue
            face, member = members[item['face_id'], item['component_id']]
            if face['units'] == 'mm':
                assert math.isclose(item['model_length_mm'], math.dist(member['a'], member['b']), abs_tol=1e-8)
                verified += 1
            else:
                assert item['model_length_mm'] is None
    errors = []; external = []; failed = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True,
            args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: external.append(r.url) if urlsplit(r.url).netloc != base.netloc else None)
        page.on('response', lambda r: failed.append([r.status, r.url]) if r.status >= 400 else None)
        page.goto(args.url)
        page.wait_for_function('window.assemblyReady === true', timeout=60000)
        assert page.locator('#final-bom').get_attribute('href') == f'/assemblies/{aid}/final-bom/'
        assert page.locator('[data-result-key="finalbom"]').get_attribute('href') == f'/assemblies/{aid}/final-bom/'
        page.goto(url)
        assert page.locator('#bom tbody tr').count() == report['counts']['rows']
        assert page.locator('#bom th').last.inner_text() == '是否需要人工关注'
        mark = report['rows'][0]['mark']
        page.locator('#search').fill(mark)
        assert page.locator('#bom tbody tr:visible').count() == sum(r['mark'] == mark for r in report['rows'])
        page.screenshot(path=str(args.out/'filtered-bom.png'))
        page.locator('#search').fill('')
        page.locator('#attention').check()
        assert page.locator('#bom tbody tr:visible').count() == report['counts']['attention']
        with page.expect_download() as event:
            page.locator('a[href="final-bom.csv"]').click()
        event.value.save_as(args.out/'final-bom.csv')
        records = list(csv.reader(io.StringIO((args.out/'final-bom.csv').read_text(encoding='utf-8-sig'))))
        assert len(records) == len(report['rows']) + 1
        assert all(len(row) == len(records[0]) for row in records)
        page.locator('#bom').evaluate('e=>e.parentElement.scrollLeft=e.parentElement.scrollWidth')
        page.screenshot(path=str(args.out/'attention-column.png'))
        page.set_viewport_size({'width': 430, 'height': 850})
        page.reload()
        assert page.locator('#search').is_visible()
        page.screenshot(path=str(args.out/'mobile.png'))
        assert not errors and not external and not failed, (errors, external, failed)
        browser.close()
    after = fetch(origin + '/api/assemblies/' + aid)
    assert before['scene'] == after['scene'] and before['faces'] == after['faces']
    result = {'url': url, 'counts': report['counts'], 'verified_member_observations': verified,
              'scene_unchanged': True, 'browser_errors': errors, 'external_requests': external, 'failed_requests': failed}
    (args.out/'browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
