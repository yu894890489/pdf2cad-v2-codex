import copy
import json
import math

import fitz
import pytest

from tower_rebuild.io import file_digest, write_json
from tower_rebuild.ocr_input import (recover_text, vector_digest, separate_paths, deduplicate,
                                     insert_hidden_text, analysis_source, annotate_primitives)
from tower_rebuild.primitives import extract_primitives
from tower_rebuild.topology_geometry import native_strokes
from tower_rebuild.workflow_settings import OcrSettings


def word(quad, text='12', confidence=.99, oid='ocr_000001'):
    return {'quad_pt': quad, 'bbox_pt': [min(p[0] for p in quad), min(p[1] for p in quad), max(p[0] for p in quad), max(p[1] for p in quad)],
            'page': 1, 'text': text, 'raw_text': text, 'confidence': confidence, 'id': oid,
            'accepted': confidence >= .75, 'needs_review': confidence < .93,
            'conflicting_readings': [], 'tile_id': 'tile', 'edge_distance_px': 50}


class FakeOcr:
    def __call__(self, image, **kwargs):
        return [([[36,36],[70,36],[70,70],[36,70]], '12', .99),
                ([[120,120],[170,120],[170,145],[120,145]], 'uncertain', .6)], [0,0,0]


def test_ocr_adds_searchable_layer_without_replacing_vectors_or_source_bytes(tmp_path):
    source = tmp_path / 'source.pdf'
    with fitz.open() as d:
        p = d.new_page(width=200, height=200)
        p.draw_line((20,20), (25,30))
        p.draw_line((10,25), (100,25))
        d.save(source)
    before = source.read_bytes()
    report = recover_text(source, tmp_path / 'ocr', OcrSettings(render_scale=2), FakeOcr())
    assert source.read_bytes() == before
    assert report['source_bytes_unchanged']
    assert report['counts']['accepted'] == 1
    assert report['counts']['review_words'] == 1
    assert report['pages'][0]['all_vector_geometry_unchanged']
    assert report['pages'][0]['visible_pixels_unchanged']
    assert report['pages'][0]['analysis_paths'] == report['pages'][0]['original_paths'] == 2
    with fitz.open(report['analysis_source']) as d:
        assert '12' in d[0].get_text()
        assert 'uncertain' not in d[0].get_text()
        records, _ = extract_primitives(d[0], 1, [])
    old = copy.deepcopy(records)
    annotate_primitives(records, report, 1)
    assert len(records) == len(old)
    assert all(a.get('commands') == b.get('commands') for a,b in zip(records, old))
    assert records[0]['role'] == 'ocr_text_stroke'
    assert not records[1].get('excluded_from_structure')
    strokes = native_strokes(records)
    assert len(strokes) == 1 and strokes[0]['primitive_ids'] == [records[1]['id']]
    assert next(p for p in records if p['primitive_type']=='text')['text_source']=='ocr'


def test_text_separation_preserves_crossing_lines_circles_and_uncertain_paths():
    with fitz.open() as d:
        p=d.new_page(width=200,height=200)
        p.draw_line((45,45),(48,50))
        p.draw_line((30,50),(90,50))
        p.draw_circle((50,50),4)
        before=vector_digest(p)
        observations=[word([[40,40],[60,40],[60,60],[40,60]])]
        excluded, ambiguous=separate_paths(p,observations,1)
        assert [e['source_path_index'] for e in excluded]==[0]
        assert vector_digest(p)==before
        observations[0]['conflicting_readings']=['13']
        excluded, ambiguous=separate_paths(p,observations,1)
        assert not excluded and ambiguous[0]['source_path_index']==0


def test_overlapping_tile_readings_keep_conflicts_and_do_not_duplicate_text():
    a=word([[10,10],[30,10],[30,20],[10,20]],'125')
    b=word([[10,10],[31,10],[31,20],[10,20]],'123',.95)
    result=deduplicate([a,b])
    assert len(result)==1 and result[0]['text']=='125'
    assert result[0]['conflicting_readings']==['123']


def test_small_segment_continuing_a_structure_line_is_not_erased():
    with fitz.open() as d:
        p=d.new_page(width=200,height=200)
        p.draw_line((45,45),(55,45))
        p.draw_line((55,45),(150,45))
        excluded, ambiguous=separate_paths(p,[word([[40,40],[60,40],[60,60],[40,60]])],1)
        assert not excluded
        assert ambiguous[0]['retention_reason']=='connected_to_long_structure'


def test_hidden_text_keeps_rotated_dimension_orientation():
    with fitz.open() as d:
        p=d.new_page(width=200,height=200)
        observation=word([[50,50],[90,30],[95,40],[55,60]], '2800')
        insert_hidden_text(p,observation)
        reopened=fitz.open(stream=d.tobytes(),filetype='pdf')
        line=reopened[0].get_text('dict')['blocks'][0]['lines'][0]
        assert ''.join(s['text'] for s in line['spans'])=='2800'
        assert line['dir']==pytest.approx((2/math.sqrt(5),-1/math.sqrt(5)),abs=.001)
        reopened.close()


def test_unicode_specification_fits_its_original_ocr_box():
    with fitz.open() as d:
        p=d.new_page(width=200,height=200)
        insert_hidden_text(p,word([[20,20],[100,20],[100,32],[20,32]],'Q355Φ219X6'))
        reopened=fitz.open(stream=d.tobytes(),filetype='pdf')
        value=reopened[0].get_text('words')[0]
        assert value[4]=='Q355Φ219X6'
        assert value[:4]==pytest.approx((20,20,100,32),abs=.1)
        reopened.close()


def test_analysis_pdf_and_ocr_evidence_cannot_be_swapped_silently(tmp_path):
    original=tmp_path/'source.pdf'; analysis=tmp_path/'analysis.pdf'; report=tmp_path/'report.json'
    original.write_bytes(b'original'); analysis.write_bytes(b'derived'); write_json(report,{'words':[]})
    manifest={'source':str(original),'source_sha256':file_digest(original),
              'analysis_source':str(analysis),'analysis_source_sha256':file_digest(analysis),
              'ocr_report':str(report),'ocr_report_sha256':file_digest(report)}
    assert analysis_source(manifest)==str(analysis)
    analysis.write_bytes(b'changed')
    with pytest.raises(ValueError,match='OCR'): analysis_source(manifest)
    analysis.write_bytes(b'derived');write_json(report,{'words':['altered']})
    with pytest.raises(ValueError,match='OCR'): analysis_source(manifest)


def test_cell_ocr_omits_compound_borders_without_clipping_glyphs_or_neighbors():
    from tower_rebuild.ocr_input import cell_ink_paths, render_cell
    with fitz.open() as doc:
        page = doc.new_page(width=100, height=100)
        shape = page.new_shape()
        shape.draw_polyline([(0, 0), (0, 80), (80, 80)])
        shape.finish(color=(0, 0, 0), closePath=False)
        shape.commit()
        page.draw_line((18, 8), (21, 10))  # glyph extends past x=20 cell border
        page.draw_line((23, 8), (28, 10))  # next cell's ink must stay separate
        original = vector_digest(page)
        paths = cell_ink_paths(page)
        assert len(paths) == 2
        pixels, x, y = render_cell(paths, (0, 0, 20, 20), 3)
        assert x == 17*3
        assert pixels.shape[1] == 5*3  # includes full glyph and 1 pt padding
        assert pixels.min() < 180
        assert vector_digest(page) == original and len(page.get_drawings()) == 3


def test_table_cell_keeps_conflicting_independent_readings_for_review():
    from tower_rebuild.ocr_input import retain_cell_alternatives
    cell = word([[10, 10], [60, 10], [60, 20], [10, 20]], 'Q3550219X6')
    tile = word([[9, 9], [61, 9], [61, 21], [9, 21]], 'Q355Φ219X6')
    merged = word([[9, 9], [120, 9], [120, 21], [9, 21]], '301 Q355Φ219X6')
    retain_cell_alternatives([cell], [tile, merged])
    result = deduplicate([cell])
    assert result[0]['text'] == 'Q3550219X6'
    assert result[0]['conflicting_readings'] == ['Q355Φ219X6']


def test_rejected_ocr_cannot_be_configured_to_bypass_review():
    with pytest.raises(ValueError, match='review_confidence'):
        OcrSettings(min_confidence=.95, review_confidence=.8)
