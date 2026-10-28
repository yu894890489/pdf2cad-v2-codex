"""Check a real generated review page; this does not certify drawing accuracy.

Requires the optional Playwright package and an installed Chromium browser.
"""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766/")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--browser", type=Path, default=Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe")
    args = parser.parse_args()
    origin = urlsplit(args.url)
    if origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Use the local viewer URL.")
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(args.browser), headless=True,
                                              args=["--disable-gpu", "--disable-webgl"])
        page = browser.new_page(viewport={"width": 1480, "height": 1050})
        errors, failed, remote = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: failed.append(request.url))
        page.on("request", lambda request: remote.append(request.url)
                if urlsplit(request.url).netloc != origin.netloc else None)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_function("window.towerViewerReady === true")
        assert page.locator("#error").is_hidden()
        model = page.request.get(urljoin(args.url, "model.json")).json()
        report = page.request.get(urljoin(args.url, "report.json")).json()
        assert model["status"] == report["status"]
        assert model["result_sha256"] == report["result_sha256"]
        assert page.locator("#members button").count() == len(model["members"])
        source_count = 0
        for face in model["faces"]:
            page.select_option("#face", face["id"])
            expected = sum(face["id"] in member["faces"] for member in model["members"])
            assert page.locator("#members button").count() == expected
            if not face.get("source_image"):
                continue
            for overlay in (False, True):
                page.locator("#overlay").set_checked(overlay)
                image = face.get("overlay_image", face["source_image"]) if overlay else face["source_image"]
                page.wait_for_function("expected => { const image=document.getElementById('source'); return image.src === expected && image.complete && image.naturalWidth>0; }",
                                       arg=urljoin(args.url, image))
            if face.get("overlay_kind") == "observations":
                assert "二维识别结果" in page.locator("#source-title").inner_text()
            source_count += 1
        if not model["members"]:
            assert page.locator("#axes-download").is_hidden()
            assert "尚未生成三维杆件" in page.locator("#selection").inner_text()
        if model["faces"]:
            page.select_option("#face", model["faces"][0]["id"])
        page.screenshot(path=str(args.out / "browser-checked.png"), full_page=True)
        assert not errors and not failed and not remote, (errors, failed, remote)
        result = {"status": model["status"], "bom_rows": len(model["bom"]),
                  "required_angle_instances": sum(row["quantity"] for row in model["bom"] if row["classification"]["kind"] == "angle"),
                  "rendered_members": len(model["members"]), "faces_checked": len(model["faces"]),
                  "source_images_checked": source_count, "errors": errors, "failed_requests": failed,
                  "remote_requests": remote, "webgl_disabled": True}
        page.route("**/model.json", lambda route: route.fulfill(status=404, body="missing"))
        page.reload()
        page.wait_for_selector("#error", state="visible")
        assert "HTTP 404" in page.locator("#error").inner_text()
        result["missing_model_has_explicit_error"] = True
        (args.out / "browser-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
