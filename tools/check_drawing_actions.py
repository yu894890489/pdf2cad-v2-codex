"""Check original processing tools from workbench, multi-PDF and saved viewers."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    base='http://127.0.0.1:8774'
    project='8b085ef72bac48f3a9538e9433339b0b'
    ten='ba843caabbf04e1091683f641cc5d352';eleven='a529e1cf815145db9f1431f62edeeb90'
    incomplete='0afb9863ad0a4cc99fb32c290a4dc74d';single='e1b0f42a9be84129b5ff15542c08e53b'
    required=['automaticassembly','topology','final','tables','audit','modelaudit','report','inputreport','bom','model','source','log']
    output=Path('output/drawing-actions-check');output.mkdir(parents=True,exist_ok=True)
    errors=[];failed=[];checks=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,
            args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1650,'height':1040})
        context.set_default_timeout(90000)
        context.on('page',lambda page:page.on('pageerror',lambda error:errors.append(str(error))))
        context.on('response',lambda r:failed.append([r.status,r.url]) if r.status>=400 else None)
        jobs={j['id']:j for j in context.request.get(base+'/api/jobs',timeout=180000).json()['jobs']}
        verified=set()
        def verify_links(page,selector,jid,keys=required):
            job=jobs[jid]
            for key in keys:
                link=page.locator(selector+f' a[data-result-key="{key}"]')
                assert link.count()==1,(selector,key)
                assert link.get_attribute('href')==job['links'][key],(jid,key)
                target=base+job['links'][key]
                if target not in verified:
                    response=context.request.head(target,timeout=180000)
                    assert response.ok,(key,response.status)
                    verified.add(target)
            checks.append({'source':jid,'buttons':keys})
        page=context.new_page();page.goto(base,timeout=180000);page.wait_for_function('window.studioReady===true',timeout=180000)
        row=f'[data-assembly-id="{project}"]'
        page.locator(row+' select').select_option(ten)
        verify_links(page,row+' .actions',ten)
        page.locator(row).screenshot(path=str(output/'workbench-project.png'))
        page.locator(row+' select').select_option(eleven)
        verify_links(page,row+' .actions',eleven)
        verify_links(page,f'[data-job-id="{single}"] .actions',single)
        page.goto(f'{base}/assemblies/{project}/',timeout=180000)
        page.wait_for_function('window.assemblyReady===true',timeout=180000)
        scene=page.evaluate('assemblyData.scene')
        for jid in (ten,eleven):
            page.locator('#drawing-source').select_option(jid)
            page.wait_for_function('(id)=>document.querySelector("#drawing-actions a[data-result-key=topology]")?.getAttribute("href").includes(id)',arg=jid)
            verify_links(page,'#drawing-actions',jid)
        page.locator('#drawing-source').select_option(incomplete)
        page.wait_for_function('(id)=>document.querySelector("#drawing-actions a[data-result-key=source]")?.getAttribute("href").includes(id)',arg=incomplete)
        assert page.locator('#drawing-actions button[data-result-key="topology"]').is_disabled()
        assert page.locator('#drawing-actions a[data-result-key="inputreport"]').count()==1
        # A picked structural face switches the source, including after a PDF
        # with no model was explicitly selected in the toolbar.
        # Use the actual face-list button, not a hidden test-only interface.
        page.locator('#faces .face-row').filter(has_text='_10.pdf').first.locator('button').click()
        page.wait_for_function('(id)=>document.getElementById("drawing-source").value===id',arg=ten)
        verify_links(page,'#drawing-actions',ten)
        # Intercept only this browser's retry request; do not reprocess or
        # replace the user's existing source/model as part of a UI check.
        retries=[]
        def retry(route):
            retries.append(route.request.url)
            route.fulfill(status=202,content_type='application/json',body=json.dumps({'id':'test-retry','name':'test.pdf'}))
        page.route(f'{base}/api/jobs/{ten}/retry',retry)
        page.locator('#drawing-actions [data-result-key="retry"]').click()
        page.wait_for_function('document.getElementById("drawing-status").textContent.includes("已创建重新处理任务")')
        assert retries==[f'{base}/api/jobs/{ten}/retry']
        assert page.evaluate('assemblyData.scene')==scene
        page.locator('#drawing-source').select_option(ten)
        page.screenshot(path=str(output/'assembly-toolbar.png'))
        page.goto(f'{base}/results/{single}/assembly/',timeout=180000)
        page.wait_for_function('window.assemblyReady===true',timeout=180000)
        page.wait_for_function('document.querySelector("#drawing-actions a[data-result-key=topology]")')
        verify_links(page,'#drawing-actions',single)
        assert page.locator('#drawing-actions [data-result-key="retry"]').is_disabled()
        page.screenshot(path=str(output/'saved-toolbar.png'))
        assert not errors and not failed,(errors,failed)
        result={'checks':checks,'source_switching':True,'missing_result_disabled':True,'retry_targets_current_source':True,
                'scene_unchanged':True,'errors':errors,'failed_requests':failed}
        (output/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False));browser.close()


if __name__=='__main__':main()
