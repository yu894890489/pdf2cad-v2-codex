"""Run explicitly against the local demo server; uses installed Chrome."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:8766/")
args = parser.parse_args()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True,
                                        args=["--disable-gpu", "--disable-webgl"])
    page = browser.new_page(viewport={"width": 1460, "height": 1000})
    errors, failed, remote = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("requestfailed", lambda r: failed.append(r.url))
    page.on("request", lambda r: remote.append(r.url) if not r.url.startswith("http://127.0.0.1:") else None)
    page.goto(args.url, wait_until="networkidle")
    page.wait_for_function("window.towerViewerReady === true")
    assert page.locator("#error").is_hidden()
    assert page.locator("#members button").count() == 13
    for view in ["front", "rear", "top", "bottom", "left", "right"]:
        page.select_option("#face", view)
        assert page.locator("#members button").count() >= 4
    page.select_option("#face", "all")
    page.locator("#search").fill("101")
    assert page.locator("#members button").count() == 4
    page.locator("#members button").first.click()
    assert "BOM 下料 2400" in page.locator("#selection").inner_text()
    page.locator("#search").fill("")
    canvas = page.locator("#model")
    bounds = canvas.bounding_box()
    page.mouse.move(bounds["x"]+300, bounds["y"]+200)
    page.mouse.down()
    page.mouse.move(bounds["x"]+450, bounds["y"]+240, steps=10)
    page.mouse.up()
    page.locator("#reset").click()
    screenshot = ROOT / "runs/box-demo/browser-checked.png"
    page.screenshot(path=str(screenshot), full_page=True)
    assert not errors and not failed and not remote, (errors, failed, remote)
    print(json.dumps({"members": 13, "faces": 6, "webgl_disabled": True, "errors": errors,
                      "failed_requests": failed, "remote_requests": remote, "screenshot": str(screenshot)}, ensure_ascii=False))
    # A missing JSON must produce an explicit error instead of an endless spinner.
    page.route("**/model.json", lambda route: route.fulfill(status=404, body="missing"))
    page.reload()
    page.wait_for_selector("#error", state="visible")
    assert "HTTP 404" in page.locator("#error").inner_text()
    browser.close()
