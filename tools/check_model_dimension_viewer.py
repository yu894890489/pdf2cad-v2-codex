"""Check report loading and compare real browser mesh vertices with its measurements."""
import argparse
import json
import math
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--url', required=True, help='Local model-audit page URL')
    parser.add_argument('--browser', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    report = json.loads((args.out / 'site/model-audit/report.json').read_text(encoding='utf-8'))
    model = json.loads((args.out / 'site/topology/model-preview.json').read_text(encoding='utf-8'))
    members = {r['object_id']: r for r in report['rows'] if r['kind'] == 'member_bom'}
    holes = {r['object_id']: r for r in report['rows'] if r['kind'] == 'hole_diameter'}
    plates = {r['object_id']: r for r in report['rows'] if r['kind'] == 'plate_thickness'}
    envelopes = {key: sorted(r['measured_mm'] for r in report['rows'] if r['kind'] == 'plate_envelope' and r['object_id'] == key and r['measured_mm'] is not None) for key in plates}
    errors, external, measurements = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True,
                                   args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width':1440, 'height':1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: external.append(r.url) if urlsplit(r.url).netloc != urlsplit(args.url).netloc else None)
        page.goto(args.url); page.wait_for_function('window.modelAuditReady === true')
        assert page.locator('#error').is_hidden()
        assert page.locator('#full-lengths tr').count() == sum(r['kind'] == 'member_initial_audit' and r['reference_mm'] is not None for r in report['rows'])
        assert page.locator('#rows tr').count() == report['counts'].get('difference', 0)
        page.screenshot(path=str(args.out / 'model-dimension-audit-browser.png'))
        for v in report['views']:
            page.locator('#view').select_option(v['view_id'])
            assert page.locator('#rows tr').count() == v['counts'].get('difference', 0)
        page.locator('#view').select_option(''); page.locator('#kind').select_option('member_initial_audit')
        page.locator('#status').select_option('pending')
        assert page.locator('#rows tr').count() == sum(r['kind'] == 'member_initial_audit' and r['status'] == 'pending' for r in report['rows'])
        page.set_viewport_size({'width':390, 'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(args.out / 'model-dimension-audit-mobile.png'))
        page.set_viewport_size({'width':1440, 'height':1100})
        page.goto(urljoin(args.url, '../topology/')); page.wait_for_function('window.preflightReady === true')
        assert page.locator('#model-audit-link').count() == 1
        page.locator('#model-toggle').click(); page.wait_for_function('window.modelPreview?.visible === true')
        for v in model['views']:
            page.locator(f'.view-button[data-id="{v["view_id"]}"]').click()
            page.wait_for_function('id => window.modelPreview?.stats.view_id === id', arg=v['view_id'])
            measured = page.evaluate('window.modelPreview.measureGeometry()'); measurements.append(measured)
            assert len(measured['members']) == len(v['members'])
            for m in measured['members']:
                assert abs(m['length_mm'] - members[m['id']]['measured_mm']) < .002, m
                section = next(item['section'] for item in v['members'] if item['id'] == m['id'])
                assert max(abs(a - b) for a, b in zip(m['legs_mm'], section['legs_mm'])) < .002, m
            for plate in measured['plates']:
                assert abs(plate['thickness_mm'] - plates[plate['id']]['measured_mm']) < .002, plate
                assert max(abs(a - b) for a, b in zip(plate['envelope_mm'], envelopes[plate['id']])) < .002, plate
            if v['scale']['mm_per_pt']:
                for hole in measured['holes']:
                    assert math.dist(hole['center_projection_mm'], holes[hole['id']]['geometry']['center_mm']) < .002, hole
        # A changed actual mesh must be observable even if recipe cached lengths stay unchanged.
        change = page.evaluate('''() => {
            const view=state.modelData.views.find(v=>v.members.length),layer=window.modelPreview;
            layer.setView(view,transform());const before=layer.measureGeometry().members[0].length_mm;
            const p=layer.groups.members.children[0].geometry.getAttribute('position');
            for(let i=0;i<p.count;i++)p.setZ(i,p.getZ(i)*.9);p.needsUpdate=true;
            return {before,after:layer.measureGeometry().members[0].length_mm};
        }''')
        assert abs(change['after'] / change['before'] - .9) < 1e-6
        # Stale data must produce a visible error rather than a perpetual loading message.
        page.route('**/topology/model-preview.json', lambda route: route.fulfill(response=route.fetch(), body=(args.out / 'site/topology/model-preview.json').read_text(encoding='utf-8') + '\n'))
        page.goto(args.url); page.wait_for_selector('#error', state='visible')
        assert '过期' in page.locator('#error').inner_text()
        assert '未加载' in page.locator('#summary').inner_text()
        assert not errors and not external, (errors, external)
        browser.close()
    result = {'views_checked': len(measurements), 'member_meshes': sum(len(v['members']) for v in measurements),
              'plate_meshes': sum(len(v['plates']) for v in measurements), 'hole_centers': sum(len(v['holes']) for v in measurements),
              'numeric_comparison_tolerance_mm': .002, 'mesh_mutation_detected': True, 'stale_report_rejected': True,
              'mobile_layout': True, 'browser_errors': errors, 'external_requests': external,
              'engineering_accuracy_verified': False, 'measurements': measurements}
    (args.out / 'model-dimension-browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print({key: value for key, value in result.items() if key != 'measurements'}, flush=True)


if __name__ == '__main__':
    main()
