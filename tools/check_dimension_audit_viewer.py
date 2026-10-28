"""Check the local dimension audit UI, evidence images, filters and failure states."""
import argparse
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--url')
    parser.add_argument('--browser',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args=parser.parse_args();server=None
    report=json.loads((args.out/'site/audit/report.json').read_text(encoding='utf-8'))
    if not args.url:
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str((args.out/'site').resolve())))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        args.url=f'http://127.0.0.1:{server.server_port}/audit/'
    errors=[];external=[];failed=[];checked=[];http_errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=args.browser,headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('requestfailed',lambda r:failed.append(r.url))
            page.on('response',lambda r:http_errors.append({'url':r.url,'status':r.status}) if r.status>=400 else None)
            page.on('request',lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
            page.goto(args.url,wait_until='networkidle',timeout=30000)
            page.wait_for_function('window.auditReady === true')
            assert page.evaluate('window.auditData.source_sha256')==report['source_sha256']
            assert page.locator('#error').is_hidden()
            assert page.locator('#polygons .card').count()==report['counts']['dimension_polygons']
            assert page.locator('#view-summaries tr').count()==len(report['views'])
            first=report['view_summaries'][0]
            page.locator('#view-summaries button').first.click()
            assert page.locator('#view').input_value()==first['view_id']
            assert page.locator('#view-summaries tr').count()==1
            assert first['conclusion'] in page.locator('#view-summaries').inner_text()
            page.select_option('#view','')
            try:page.wait_for_function("[...document.images].every(i=>i.complete && i.naturalWidth>0)",timeout=10000)
            except Exception:
                print(json.dumps({'images':page.evaluate("[...document.images].filter(i=>!i.complete||!i.naturalWidth).map(i=>({src:i.src,complete:i.complete,width:i.naturalWidth}))"),'failed':failed,'http_errors':http_errors},ensure_ascii=False))
                raise
            page.screenshot(path=str(args.out/'dimension-audit-browser.png'))
            for view in report['views']:
                page.select_option('#view',view['id'])
                expected=lambda name:[r for r in report[name] if r['view_id']==view['id']]
                assert page.locator('#polygons .card').count()==len(expected('polygons'))
                assert page.locator('#findings .finding').count()==len(expected('findings'))
                assert page.locator('#annotations tr').count()==len(expected('annotations'))
                summary=next(s for s in report['view_summaries'] if s['view_id']==view['id'])
                assert page.locator('#view-summaries tr').count()==1
                assert summary['conclusion'] in page.locator('#view-summaries').inner_text()
                pending=[s for s in expected('span_bom_checks') if 'formula' not in s]
                assert page.locator('#pending-spans tr').count()==len(pending)
                for s in pending:
                    for reason in s['pending_reasons']:assert reason['message'] in page.locator('#pending-spans').text_content()
                decisions=[f['decision'] for f in expected('findings') if 'decision' in f]
                assert page.locator('#findings .table tbody tr').count()==sum(len(d['hypotheses']) for d in decisions)
                for decision in decisions:
                    for hypothesis in decision['hypotheses']:
                        assert hypothesis['label'] in page.locator('#findings').inner_text()
                    if decision['preferred']:assert '优先核对候选' in page.locator('#findings').inner_text()
                spans=[s for s in expected('span_bom_checks') if 'formula' in s]
                assert page.locator('#spans .card').count()==len(spans)
                for s in spans:assert s['formula'] in page.locator('#spans').inner_text()
                page.wait_for_function("[...document.images].every(i=>i.complete && i.naturalWidth>0)")
                if '1—1' in view['title']:
                    page.screenshot(path=str(args.out/'dimension-audit-section11.png'),full_page=True)
                    page.locator('#spans').screenshot(path=str(args.out/'dimension-audit-span-detail.png'))
                checked.append(view['id'])
            page.select_option('#view','')
            for a in report['annotations']:
                if a['association_status']=='ambiguous':assert '圈号对应有多解' in page.locator('#annotations').inner_text()
                if not a['outline_direction_agrees']:assert '已识别杆件方向不符' in page.locator('#annotations').inner_text()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            page.screenshot(path=str(args.out/'dimension-audit-mobile.png'))
            page.set_viewport_size({'width':1440,'height':1100})
            page.locator('header a').click()
            page.wait_for_function('window.topologyReady === true',timeout=30000)
            page.locator('#audit-link').click()
            page.wait_for_function('window.auditReady === true')
            assert not errors and not external and not failed,(errors,external,failed)
            # A lost crop must expose a bounded failure state with a working retry.
            sample=report['polygons'][0]['image'];pattern=f'**/audit/{sample}'
            page.route(pattern,lambda route:route.fulfill(status=404,body='missing image'))
            page.reload();page.wait_for_function('window.auditReady === true')
            card=page.locator(f'#polygons .card:has(img[alt="{report["polygons"][0]["id"]}"])')
            card.locator('.image-error').wait_for(state='visible')
            page.unroute(pattern);card.locator('button').click()
            page.wait_for_function('src=>[...document.images].filter(i=>i.src.includes(src+"?retry=")).every(i=>i.complete&&i.naturalWidth>0)',arg=sample)
            assert card.locator('.image-error').is_hidden()
            page.route('**/audit/report.json',lambda route:route.fulfill(status=404,body='missing'))
            page.reload();page.wait_for_selector('#error',state='visible')
            assert 'HTTP 404' in page.locator('#error').inner_text()
            page.unroute('**/audit/report.json')
            page.route('**/audit/report.json',lambda route:route.fulfill(status=200,body='invalid JSON'))
            page.reload();page.wait_for_selector('#error',state='visible')
            assert page.locator('#summary').inner_text()=='加载失败'
            browser.close()
    finally:
        if server:server.shutdown();server.server_close()
    result={'views_checked':checked,'counts':report['counts'],'all_evidence_images_loaded':True,
            'javascript_errors':errors,'external_requests':external,'failed_requests':failed,
            'mobile_no_page_overflow':True,'navigation_round_trip':True,'missing_json_error_visible':True,
            'invalid_json_error_visible':True,'image_failure_and_retry_verified':True,'engineering_accuracy_verified':False}
    (args.out/'dimension-audit-browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"{len(checked)} views verified; images, formulas, mobile, navigation, missing/invalid data states passed; local-only")


if __name__=='__main__':main()
