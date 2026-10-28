import json
from pathlib import Path
import time
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import fitz
import pytest

from tower_rebuild.io import file_digest, read_json, write_json
from tower_rebuild.studio import create_studio, JobStore
from tower_rebuild.workflow import process_drawing


def empty_pdf(path):
    with fitz.open() as doc:
        doc.new_page()
        doc.save(path)
    return path


def test_unsupported_input_is_retained_and_does_not_report_a_model(tmp_path):
    pdf = empty_pdf(tmp_path / 'scan.pdf')
    result = process_drawing(pdf, tmp_path / 'out')
    assert result['status'] == 'unsupported'
    assert result['source_sha256'] == file_digest(pdf)
    assert (tmp_path / 'out/input/source.pdf').read_bytes() == pdf.read_bytes()
    assert not (tmp_path / 'out/site/topology/model-preview.json').exists()
    assert result['error']['stage'] == 'input'
    assert not result['engineering_accuracy_verified']


def test_pipeline_refuses_to_replace_any_existing_run(tmp_path):
    pdf = empty_pdf(tmp_path / 'source.pdf')
    out = tmp_path / 'old'
    out.mkdir()
    marker = out / 'workflow.json'
    marker.write_text('{"source":"previous"}')
    with pytest.raises(ValueError, match='输出目录'):
        process_drawing(pdf, out)
    assert marker.read_text() == '{"source":"previous"}'
    assert list(out.iterdir()) == [marker]


def test_vector_without_text_reports_text_recovery_and_keeps_page_evidence(tmp_path):
    pdf = tmp_path / 'outlined.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        page.draw_line((20, 20), (80, 80))
        doc.save(pdf)
    result = process_drawing(pdf, tmp_path / 'out', config={'ocr': {'mode': 'disabled'}})
    assert result['status'] == 'unsupported'
    assert result['error']['code'] == 'vector_without_text'
    assert 'BOM' in result['error']['message']
    assert '扫描' not in result['error']['message']
    assert result['pages'][0]['native_paths'] == 1
    assert result['pages'][0]['text_characters'] == 0
    assert result['pages'][0]['raster_images'] == 0
    assert result['input_coverage']['pages_with_coverage_issues'] == 1
    report = (tmp_path / 'out/input-review.md').read_text(encoding='utf-8')
    assert '原生路径 1' in report and '文字字符 0' in report


def test_image_only_pdf_is_distinguished_from_vector_text_outlines(tmp_path):
    pdf = tmp_path / 'image-only.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        bitmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
        bitmap.clear_with(255)
        page.insert_image(page.rect, pixmap=bitmap)
        doc.save(pdf)
    result = process_drawing(pdf, tmp_path / 'out')
    assert result['error']['code'] == 'raster_without_vectors'
    assert result['pages'][0]['native_paths'] == 0
    assert result['pages'][0]['raster_images'] == 1
    assert result['pages'][0]['largest_raster_fraction'] == pytest.approx(1)


def test_failed_input_gets_a_diagnostic_instead_of_a_loading_state(tmp_path):
    state = process_drawing(tmp_path / 'missing.pdf', tmp_path / 'run')
    assert state['status'] == 'failed'
    assert state['stages'][0]['status'] == 'failed'
    assert read_json(tmp_path / 'run/failure.json')['message']
    assert not (tmp_path / 'run/.workflow.lock').exists()


def test_server_restart_marks_inflight_jobs_interrupted(tmp_path):
    write_json(tmp_path / 'abc/job.json', {'id': 'abc', 'name': 'a.pdf', 'status': 'running', 'created_at': '2026'})
    store = JobStore(tmp_path)
    try:
        assert store.snapshot('abc')['status'] == 'interrupted'
    finally:
        store.close()


@pytest.fixture
def studio(tmp_path):
    server, store = create_studio(tmp_path / 'studio', 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', store
    server.shutdown()
    server.server_close()
    store.close()
    worker.join()


def call(url, data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=20) as response:
        return json.loads(response.read())


def test_upload_requires_session_and_rejects_non_pdf(studio):
    base, store = studio
    token = call(base + '/api/session')['token']
    for headers in ({}, {'Origin': 'https://example.com'}):
        with pytest.raises(HTTPError) as error:
            call(base + '/api/jobs', b'%PDF-fake', headers)
        assert error.value.code == 403
    with pytest.raises(HTTPError) as error:
        call(base + '/api/jobs', b'not a PDF', {'X-Upload-Token': token, 'X-File-Name': 'x.pdf'})
    assert error.value.code == 400
    assert not store.listing()
    assert not list(store.root.iterdir())


def test_local_page_accepts_external_origin_header(studio):
    base, _ = studio
    for origin in ('null', 'https://example.com'):
        headers = {'Origin': origin, 'Host': '192.0.2.1:8774'}
        with urlopen(Request(base + '/', headers=headers), timeout=5) as response:
            assert response.status == 200
            assert b'<html' in response.read().lower()
        with urlopen(Request(base + '/api/session', headers=headers), timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())['token']


def test_same_named_uploads_and_retries_keep_independent_sources(studio, tmp_path):
    base, store = studio
    pdf = empty_pdf(tmp_path / 'empty.pdf').read_bytes()
    token = call(base + '/api/session')['token']
    headers = {'X-Upload-Token': token, 'X-File-Name': 'same.pdf'}
    first = call(base + '/api/jobs', pdf, headers)
    second = call(base + '/api/jobs', pdf, headers)
    assert first['id'] != second['id']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        jobs = call(base + '/api/jobs')['jobs']
        if all(j['status'] not in {'queued', 'running'} for j in jobs):
            break
        time.sleep(.1)
    assert all(j['status'] == 'unsupported' for j in jobs), jobs
    retry = call(base + f'/api/jobs/{first["id"]}/retry', b'', headers)
    assert retry['id'] not in {first['id'], second['id']}
    assert retry['retry_of'] == first['id']
    for jid in (first['id'], second['id'], retry['id']):
        assert (store.root / jid / 'source.pdf').read_bytes() == pdf
    assert call(base + f'/api/jobs/{first["id"]}')['status'] == 'unsupported'


def test_results_cannot_serve_files_above_the_generated_site(studio, tmp_path):
    base, store = studio
    jid = 'known'
    folder = store.root / jid
    (folder / 'run/site').mkdir(parents=True)
    (folder / 'source.pdf').write_bytes(b'private source')
    store.jobs[jid] = {'id': jid, 'name': 'file.pdf', 'status': 'unsupported', 'created_at': '2026'}
    for suffix in ('../source.pdf', '%2e%2e/%2e%2e/source.pdf', '%5c..%5c..%5csource.pdf'):
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/results/known/' + suffix, timeout=5)
        assert error.value.code in {403, 404}
