"""Verify a generated OCR review page against its local evidence and images."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--out', type=Path, default=Path('output/ocr-browser-check'))
    parser.add_argument('--browser', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    url = args.url.rstrip('/') + '/'
    with urlopen(url + 'report.json', timeout=30) as response:
        report = json.load(response)
    assert report['source_bytes_unchanged']
    assert all(p['all_vector_geometry_unchanged'] and p['visible_pixels_unchanged'] for p in report['pages'])
    errors, external = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser, headless=True)
        page = browser.new_page(viewport={'width':1440, 'height':1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: external.append(request.url)
                if urlsplit(request.url).netloc != urlsplit(url).netloc else None)
        page.goto(url)
        page.wait_for_function('window.ocrReady === true', timeout=60000)
        assert page.locator('#words button').count() == sum(w['page']==1 for w in report['words'])
        for layer in ('original', 'geometry', 'glyphs'):
            page.locator('#layer').select_option(layer)
            page.wait_for_function('document.getElementById("drawing").complete && document.getElementById("drawing").naturalWidth > 0')
            page.screenshot(path=str(args.out / f'{layer}.png'))
        page.locator('#words button').first.click()
        assert page.locator('#overlay polygon.active').count() == 1
        page.locator('#pending').check()
        assert page.locator('#words button').count() == sum(w['page']==1 and w['needs_review'] for w in report['words'])
        page.locator('#pending').uncheck()
        example = next(w['text'] for w in report['words'] if w['page']==1)
        page.locator('#search').fill(example)
        assert page.locator('#words button').count() > 0
        page.locator('#search').fill('')
        page.locator('#boxes').uncheck()
        assert not page.locator('#overlay').is_visible()
        page.locator('#layer').select_option('original')
        page.set_viewport_size({'width':390, 'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(args.out / 'mobile.png'))
        browser.close()
    assert not errors and not external, (errors, external)
    result = {'url':url, 'counts':report['counts'], 'pages':report['pages'],
              'source_bytes_unchanged':True, 'layer_switching':True,
              'word_selection_and_filters':True, 'mobile_layout':True,
              'browser_errors':errors, 'external_requests':external}
    (args.out / 'check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
