"""Verify every local topology view, relation layer, evidence selection and failure UI."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://127.0.0.1:8768/topology/")
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--browser",default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args=parser.parse_args()
    root=args.out/"site/topology"
    report=json.loads((root/"report.json").read_text(encoding="utf-8"))
    views=json.loads((root/"views.json").read_text(encoding="utf-8"))
    data={k:json.loads((root/f"{k}.json").read_text(encoding="utf-8")) for k in ("members","plates","holes")}
    assert all(report["checks"].values())
    errors=[];failed=[];external=[];checked=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.browser,headless=True,args=["--disable-webgl","--disable-gpu"])
        page=browser.new_page(viewport={"width":1540,"height":1100})
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.on("requestfailed",lambda r:failed.append(r.url))
        page.on("request",lambda r:external.append(r.url) if urlsplit(r.url).netloc!=urlsplit(args.url).netloc else None)
        page.goto(args.url,wait_until="networkidle",timeout=60000)
        page.wait_for_function("window.topologyReady === true",timeout=60000)
        assert page.locator("#error").is_hidden()
        page.screenshot(path=str(args.out/"topology-browser.png"),full_page=True)
        for view in views:
            page.locator(f'.view-button[data-id="{view["id"]}"]').click()
            page.wait_for_function("id => window.topologyState.images.has(id) && document.getElementById('view-title').textContent === window.topologyState.view.title",arg=view["id"])
            assert page.locator("#view-title").inner_text()==view["title"]
            for stage in ("members","plates","holes"):
                page.locator(f'[data-stage="{stage}"]').click()
                expected=[r for r in data[stage] if r["view_id"]==view["id"]]
                assert page.locator("#records tr").count()==len(expected),(view["id"],stage)
                if expected:
                    page.locator("#records button").first.click()
                    assert expected[0]["id"] in page.locator("#selected").inner_text()
            for layer in ("raw-page","ink","crossings","background","overlap-layer"):
                page.locator(f"#{layer}").click()
                page.locator(f"#{layer}").click()
            page.locator("#fit").click()
            if view["role"]=="detail" and len(view["hole_ids"])==2:
                assert "21.5" in page.locator("#records").inner_text()
                page.screenshot(path=str(args.out/"topology-hole-detail-browser.png"),full_page=True)
            checked.append({"view":view["id"],"title":view["title"],"members":len(view["member_ids"]),"plates":len(view["plate_ids"]),"holes":len(view["hole_ids"])})
        recovered=[m for m in data["members"] if m.get("overlap_recovery",{}).get("applied")]
        overlap_checks=[]
        for member in recovered:
            page.evaluate("async id => {await chooseView(id);}",member["view_id"])
            page.locator('[data-stage="members"]').click()
            page.locator(f'#records button[data-id="{member["id"]}"]').click()
            assert "补全段及依据" in page.locator("#selected").inner_text()
            assert "待核对" in page.locator("#selected").inner_text()
            before=page.locator("#drawing").screenshot()
            page.locator("#overlap-layer").uncheck()
            after=page.locator("#drawing").screenshot()
            assert before!=after,member["id"]
            page.locator("#overlap-layer").check()
            overlap_checks.append(member["id"])
        # Record a reproducible close-up of the first recovered paired member.
        sample=next((m for m in recovered if m["mark"]=="412"),recovered[0] if recovered else None)
        if sample:
            page.evaluate("async id => {await chooseView(id);}",sample["view_id"])
            page.locator('[data-stage="members"]').click()
            page.locator(f'#records button[data-id="{sample["id"]}"]').click()
            page.evaluate("""() => {
                state.zoom=2.5;
                const axis=state.selected.axis, t=transform();
                const mid=axis.a.map((v,i)=>(v+axis.b[i])/2);
                state.pan=[t.w/2-mid[0]*t.k-t.ox,t.h/2-mid[1]*t.k-t.oy];render();
            }""")
            page.screenshot(path=str(args.out/"topology-overlap-browser.png"),full_page=True)
            page.locator("#drawing").screenshot(path=str(args.out/"topology-overlap-detail.png"))
        section=next((v for v in views if v["title"]=="剖面 1—1"),None)
        yellow_checks=[]
        if section:
            page.evaluate("async id => {await chooseView(id);}",section["id"])
            page.locator('[data-stage="members"]').click()
            page.locator("#contacts").uncheck()
            yellow_checks=page.evaluate("""() => {
                const canvas=document.getElementById('drawing'),ctx=canvas.getContext('2d'),t=transform();
                const dpr=devicePixelRatio||1,pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
                const yellow=p=>{
                    const x=Math.round((p[0]*t.k+t.ox)*dpr),y=Math.round((p[1]*t.k+t.oy)*dpr);
                    for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++){
                        const i=((y+dy)*canvas.width+x+dx)*4;
                        if(pixels[i]===230 && pixels[i+1]===184 && pixels[i+2]===0)return true;
                    }return false;
                };
                return list('members').filter(m=>m.overlap_recovery?.applied).map(m=>({
                    id:m.id,continuous_yellow:[.02,.1,.25,.5,.75,.9,.98].every(k=>yellow(m.axis.a.map((v,i)=>v+(m.axis.b[i]-v)*k))),
                    extension_yellow:m.overlap_recovery.extensions.every(e=>yellow(e.a.map((v,i)=>(v+e.b[i])/2)))
                }));
            }""")
            assert all(c["continuous_yellow"] and c["extension_yellow"] for c in yellow_checks),yellow_checks
            page.screenshot(path=str(args.out/"topology-section11-browser.png"),full_page=True)
            page.locator("#drawing").screenshot(path=str(args.out/"topology-section11-detail.png"))
        assert not errors and not failed and not external,(errors,failed,external)
        page.route("**/topology/report.json",lambda route:route.fulfill(status=404,body="missing"))
        page.reload()
        page.wait_for_selector("#error",state="visible")
        assert "HTTP 404" in page.locator("#error").inner_text()
        browser.close()
    result={"checked_views":checked,"integrity_checks":report["checks"],"javascript_errors":errors,"failed_requests":failed,
            "external_requests":external,"overlap_toggle_checked":overlap_checks,
            "section11_yellow_continuity_checks":yellow_checks,
            "missing_data_error_visible":True,"engineering_accuracy_verified":False}
    (args.out/"topology-browser-check.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"{len(checked)} views x 3 stages passed; local-only requests; clear missing-data failure")


if __name__=="__main__":
    main()
