"""Exercise the local table/primitive viewer against its actual saved arrays."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8768/")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--browser", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args = parser.parse_args()
    root = args.out / "site"
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert all(summary["checks"].values())
    for page in summary["pages"]:
        with Image.open(root / page["original"]) as original, Image.open(root / page["tables"]) as tables, Image.open(root / page["structure"]) as structure:
            assert ImageChops.difference(original, ImageChops.darker(tables, structure)).getbbox() is None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True, args=["--disable-gpu", "--disable-webgl"])
        page = browser.new_page(viewport={"width": 1540, "height": 1050})
        errors, failed, external = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: failed.append(request.url))
        page.on("request", lambda request: external.append(request.url) if urlsplit(request.url).netloc != urlsplit(args.url).netloc else None)
        page.goto(args.url, wait_until="networkidle", timeout=60000)
        page.wait_for_function("window.decompositionReady === true", timeout=60000)
        assert page.locator("#error").is_hidden()
        notes = page.locator("#notes").text_content()
        for mark in summary.get("circle_marks_not_in_bom", []):
            assert mark in notes
        assert str(summary["structure_primitive_count"]) in page.locator("#visible-count").inner_text().replace(",", "")
        for kind, count in summary["primitive_types"].items():
            page.locator(f'#types button[data-kind="{kind}"]').click()
            assert str(count) in page.locator("#visible-count").inner_text().replace(",", "")
        page.locator('#types button[data-kind="all"]').click()
        page.select_option("#role", "member_callout")
        assert str(summary["roles"]["member_callout"]) in page.locator("#visible-count").inner_text().replace(",", "")
        page.select_option("#role", "all")
        page.screenshot(path=str(args.out / "structure-browser.png"), full_page=True)
        page.locator('nav button[data-tab="tables"]').click()
        assert page.locator("#table-body tr").count() == summary["bom_rows"]
        page.screenshot(path=str(args.out / "tables-browser.png"), full_page=True)
        tables = json.loads((root / "tables/tables.json").read_text(encoding="utf-8"))
        for table in tables:
            page.select_option("#table-select", table["id"])
            assert page.locator("#table-body tr").count() == table["row_count"]
            page.locator('#tables-panel details summary').click()
            page.wait_for_function("document.getElementById('table-image').complete && document.getElementById('table-image').naturalWidth > 0")
            page.locator('#tables-panel details summary').click()
        page.select_option("#table-select", "bom")
        page.locator("#table-body button").first.click()
        assert page.locator("#structure-panel").is_visible() and page.locator("#search").input_value()
        assert "BOM对应" in page.locator("#selected").inner_text()
        box = page.locator("#drawing").bounding_box()
        page.mouse.move(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
        page.mouse.wheel(0,-600)
        page.locator("#reset").click()
        assert not errors and not failed and not external, (errors, failed, external)
        page.route("**/summary.json", lambda route: route.fulfill(status=404, body="missing"))
        page.reload()
        page.wait_for_selector("#error", state="visible")
        assert "HTTP 404" in page.locator("#error").inner_text()
        result = {"table_count":len(tables),"bom_rows":summary["bom_rows"], "structure_primitives":summary["structure_primitive_count"],
                  "primitive_type_filters":len(summary["primitive_types"]), "mask_recomposition_matches_original":True,
                  "all_integrity_checks_pass":True,"errors":errors,"failed_requests":failed,"external_requests":external,"missing_data_error_visible":True}
        (args.out / "browser-check.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
