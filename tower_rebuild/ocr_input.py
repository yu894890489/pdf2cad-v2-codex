"""Recover outlined text locally, preserving every source vector and source byte.

The derived PDF adds invisible text only. Path separation is an evidence-linked
classification; even excluded glyph paths remain in primitive JSON and the PDF.
"""
from collections import Counter, defaultdict
from pathlib import Path
import hashlib
import math
import re
import shutil
import unicodedata

import fitz
import numpy as np
from shapely.geometry import Polygon, box as shape_box
from shapely.strtree import STRtree

from .io import digest, file_digest, read_json, write_json
from .primitives import geometry, plain


VERSION = '1.0'


def vector_digest(page):
    # Text insertion changes display-list sequence numbers, not path geometry.
    return digest([{k: plain(v) for k, v in p.items() if k != 'seqno'} for p in page.get_drawings()])


def clean_text(text):
    return unicodedata.normalize('NFKC', text).strip().replace('×', 'X').replace('—', '-')


def tiles(length, size, overlap):
    if length <= size:
        return [0]
    return sorted(set([*range(0, length-size+1, size-overlap), length-size]))


def deduplicate(observations):
    """Overlapping tiles are alternate observations, never concatenate their text."""
    kept = []
    for item in sorted(observations, key=lambda x: (x['edge_distance_px'] > 12, x['confidence']), reverse=True):
        poly = Polygon(item['quad_pt'])
        duplicate = None
        for other in kept:
            if item['page'] != other['page']:
                continue
            a, b = item['bbox_pt'], other['bbox_pt']
            if a[0] > b[2] or a[2] < b[0] or a[1] > b[3] or a[3] < b[1]:
                continue
            peer = Polygon(other['quad_pt'])
            overlap = poly.intersection(peer).area / max(1e-9, min(poly.area, peer.area))
            if overlap > .65 and min(poly.area, peer.area) / max(poly.area, peer.area) > .45:
                duplicate = other
                break
        if duplicate is None:
            item['alternatives'] = list(item.get('alternatives', []))
            kept.append(item)
        else:
            duplicate['alternatives'].append({k: item[k] for k in ('text', 'confidence', 'tile_id', 'bbox_pt')})
    kept.sort(key=lambda x: (x['page'], x['bbox_pt'][1], x['bbox_pt'][0]))
    for i, item in enumerate(kept):
        item['id'] = f'ocr_{i+1:06d}'
        variants = {a['text'] for a in item['alternatives'] if a['confidence'] >= .85}
        item['conflicting_readings'] = sorted(variants - {item['text']})
    return kept


def recognize(page, page_number, engine, settings, cache):
    pix = page.get_pixmap(matrix=fitz.Matrix(settings.render_scale, settings.render_scale), alpha=False)
    bitmap = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
    xs = tiles(pix.width, settings.tile_pixels, settings.overlap_pixels)
    ys = tiles(pix.height, settings.tile_pixels, settings.overlap_pixels)
    records = []
    for yi, y in enumerate(ys):
        for xi, x in enumerate(xs):
            tile_id = f'p{page_number:03d}_{yi:02d}_{xi:02d}'
            target = cache / (tile_id + '.json')
            tile = bitmap[y:y+settings.tile_pixels, x:x+settings.tile_pixels]
            key = hashlib.sha256(tile.tobytes()).hexdigest()
            saved = read_json(target) if target.exists() else {}
            if saved.get('pixels_sha256') == key:
                values = saved['results']
            else:
                values, elapsed = engine(tile, use_cls=True, text_score=.4, box_thresh=.4)
                values = values or []
                write_json(target, {'pixels_sha256': key, 'results': values, 'seconds': elapsed})
            print(f'[ocr] {tile_id}: {len(values)} text observations', flush=True)
            for quad, raw, confidence, *unused in values:
                points = np.asarray(quad, dtype=float)
                edge = min(points[:, 0].min(), points[:, 1].min(), tile.shape[1]-points[:, 0].max(), tile.shape[0]-points[:, 1].max())
                points = (points + [x, y] + [pix.x, pix.y]) / settings.render_scale
                text = clean_text(raw)
                if not text or not Polygon(points).is_valid or Polygon(points).area <= .1:
                    continue
                records.append({'page': page_number, 'text': text, 'raw_text': raw,
                                'confidence': float(confidence), 'quad_pt': points.tolist(),
                                'bbox_pt': [*points.min(axis=0), *points.max(axis=0)],
                                'tile_id': tile_id, 'edge_distance_px': float(edge)})
    return records


def table_grids(page):
    """Repeated native row borders locate grids even without readable headers."""
    groups = defaultdict(list)
    paths = page.get_drawings()
    long_paths = [p for p in paths if max(p['rect'].width, p['rect'].height) > 25]
    for p in paths:
        if len(p['items']) != 1 or p['items'][0][0] != 'l':
            continue
        _, a, b = p['items'][0]
        if abs(a.y-b.y) < .2 and abs(a.x-b.x) > 120:
            groups[(round(min(a.x, b.x)), round(max(a.x, b.x)))].append(a.y)
    grids, seen = [], set()
    for (left, right), ys in groups.items():
        ys = sorted(set(round(y, 1) for y in ys))
        if len(ys) < 6:
            continue
        row_step = float(np.median(np.diff(ys)))
        clip = fitz.Rect(left-1.5, min(ys)-1, right+1.5, max(ys)+row_step+1) & page.rect
        finder = page.find_tables(clip=clip, paths=long_paths)
        # PyMuPDF intentionally omits text-empty tables, but retains their cells.
        cells = [list(c) for c in finder.cells]
        if len(cells) < 16:
            continue
        bbox = [min(c[0] for c in cells), min(c[1] for c in cells), max(c[2] for c in cells), max(c[3] for c in cells)]
        key = tuple(round(v, 1) for v in bbox)
        if key not in seen:
            grids.append({'bbox_pt': bbox, 'cells': cells})
            seen.add(key)
    return grids


def cell_ink_paths(page):
    """Prepare an OCR-only copy of paths; never edit the source PDF geometry."""
    result = []
    for path in page.get_drawings():
        # Borders may be separate lines or one compound polyline. Remove only
        # long axis-aligned segments, retaining other commands from that path.
        items = [item for item in path['items'] if not (
            item[0] == 'l' and abs(item[2]-item[1]) > 25 and
            (abs(item[1].x-item[2].x) < .2 or abs(item[1].y-item[2].y) < .2))]
        if not items:
            continue
        if len(items) != len(path['items']):
            points = [v for item in items for v in item[1:] if isinstance(v, fitz.Point)]
            if not points:
                continue
            path = {**path, 'items': items, 'closePath': False,
                    'rect': fitz.Rect(min(v.x for v in points), min(v.y for v in points),
                                      max(v.x for v in points), max(v.y for v in points))}
        result.append(path)
    return result


def render_cell(paths, cell, scale):
    """Assign ink by its center and render full glyphs, including border overflow."""
    x0, y0, x1, y1 = cell
    selected = [p for p in paths if
                x0 <= (p['rect'].x0+p['rect'].x1)/2 < x1 and
                y0 <= (p['rect'].y0+p['rect'].y1)/2 < y1 and
                p['rect'].width < x1-x0+4 and p['rect'].height < y1-y0+4]
    if not selected:
        return None
    bounds = fitz.Rect(min(p['rect'].x0 for p in selected)-1,
                       min(p['rect'].y0 for p in selected)-1,
                       max(p['rect'].x1 for p in selected)+1,
                       max(p['rect'].y1 for p in selected)+1)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{bounds.width}" height="{bounds.height}" '
           f'viewBox="{bounds.x0} {bounds.y0} {bounds.width} {bounds.height}">' +
           ''.join(svg_path(p) for p in selected) + '</svg>')
    with fitz.open(stream=svg.encode(), filetype='svg') as drawing:
        pix = drawing[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
    return rgb, bounds.x0*scale, bounds.y0*scale


def recognize_cells(page, page_number, engine, settings, cache):
    """Read grid cells independently to avoid merging marks with specifications."""
    grids = table_grids(page)
    key = digest({'grids': grids, 'scale': settings.render_scale, 'source_vectors': vector_digest(page), 'cell_mode': 'complete_cell_glyph_v5'})
    target = cache / f'p{page_number:03d}-cells.json'
    if target.exists():
        saved = read_json(target)
        if saved.get('key') == key:
            return saved['records'], grids
    paths = cell_ink_paths(page) if grids else []
    crops, meta, records, used = [], [], [], set()
    for grid in grids:
        for cell in grid['cells']:
            if tuple(cell) in used:
                continue
            used.add(tuple(cell))
            x0, y0, x1, y1 = cell
            scale = settings.render_scale
            rendered = render_cell(paths, cell, scale)
            if rendered is None:
                continue
            crop, xa, ya = rendered
            ink = np.min(crop, axis=2) < 180
            yy, xx = np.where(ink)
            if len(xx) < 6:
                continue
            # The tight box gives the true ink position, not the entire grid cell.
            ax, ay, bx, by = xx.min(), yy.min(), xx.max()+1, yy.max()+1
            tight = crop[ay:by, ax:bx]
            if min(tight.shape[:2]) < 3:
                continue
            padded = np.pad(tight, ((5, 5), (5, 5), (0, 0)), constant_values=255)
            bbox = [(xa+ax)/scale, (ya+ay)/scale, (xa+bx)/scale, (ya+by)/scale]
            item = {'page': page_number, 'tile_id': f'p{page_number:03d}-cell', 'edge_distance_px': 100,
                    'bbox_pt': bbox, 'quad_pt': [[bbox[0],bbox[1]],[bbox[2],bbox[1]],[bbox[2],bbox[3]],[bbox[0],bbox[3]]],
                    'cell_bbox_pt': cell, 'source_kind': 'table_cell'}
            # Split stacked headers into ink rows. Rotating a vertical Chinese
            # header as if it were sideways Latin text corrupts its characters.
            if y1-y0 > 30:
                occupied = np.any(np.min(tight, axis=2) < 180, axis=1)
                breaks = np.diff(np.r_[False, occupied, False].astype(int))
                for top, bottom in zip(np.where(breaks == 1)[0], np.where(breaks == -1)[0]):
                    if bottom-top < 4:
                        continue
                    strip = tight[top:bottom]
                    sx = np.where(np.any(np.min(strip,axis=2)<180,axis=0))[0]
                    if not len(sx):
                        continue
                    left, right = sx.min(), sx.max()+1
                    bbox = [(xa+ax+left)/scale, (ya+ay+top)/scale, (xa+ax+right)/scale, (ya+ay+bottom)/scale]
                    line = {**item, 'bbox_pt': bbox, 'quad_pt': [[bbox[0],bbox[1]],[bbox[2],bbox[1]],[bbox[2],bbox[3]],[bbox[0],bbox[3]]]}
                    crops.append(np.pad(strip[:,left:right], ((7,7),(7,7),(0,0)), constant_values=255)); meta.append(line)
            else:
                crops.append(padded); meta.append(item)
    if crops:
        print(f'[ocr] page {page_number}: recognizing {len(crops)} cell text lines', flush=True)
        values, elapsed = engine.text_rec(crops, False)
        for item, result in zip(meta, values):
            text, confidence = result[:2]
            if text.strip():
                records.append({**item, 'raw_text': text, 'text': clean_text(text), 'confidence': float(confidence)})
    write_json(target, {'key': key, 'records': records})
    print(f'[ocr] page {page_number}: {len(grids)} native grids; {len(records)} independent cell readings', flush=True)
    return records, grids


def retain_cell_alternatives(cells, tile_words):
    """Keep independent OCR readings as evidence without merging adjacent cells."""
    polygons = [Polygon(w['quad_pt']) for w in tile_words]
    tree = STRtree(polygons)
    for cell in cells:
        polygon = Polygon(cell['quad_pt'])
        cell['alternatives'] = []
        for hit in tree.query(polygon):
            candidate, peer = tile_words[int(hit)], polygons[int(hit)]
            ratio = min(polygon.area, peer.area) / max(polygon.area, peer.area, 1e-9)
            overlap = polygon.intersection(peer).area / max(1e-9, min(polygon.area, peer.area))
            if ratio > .65 and overlap > .8:
                cell['alternatives'].append({k: candidate[k] for k in ('text', 'confidence', 'tile_id', 'bbox_pt')})


def insert_hidden_text(page, observation):
    """Map font coordinates to OCR's oriented quad without painting any pixels."""
    q = np.asarray(observation['quad_pt'], dtype=float)
    horizontal = ((q[1]-q[0])+(q[2]-q[3])) / 2
    vertical = ((q[3]-q[0])+(q[2]-q[1])) / 2
    width, height = np.linalg.norm(horizontal), np.linalg.norm(vertical)
    if min(width, height) < .1:
        return
    # The detector orders tall words top-to-bottom; OCR rotates such crops.
    if height / width >= 1.5 and len(observation['text']) > 1:
        q = q[[3, 0, 1, 2]]
        horizontal, vertical = q[1]-q[0], q[3]-q[0]
        width, height = np.linalg.norm(horizontal), np.linalg.norm(vertical)
    unicode_text = any(ord(c) > 255 for c in observation['text'])
    font = fitz.Font('cjk' if unicode_text else 'helv')
    fontname = 'ocr_unicode' if unicode_text else 'helv'
    if unicode_text:
        page.insert_font(fontname=fontname, fontbuffer=font.buffer)
    text_width = font.text_length(observation['text'], fontsize=1)
    em_height = font.ascender - font.descender
    u, v = horizontal / max(text_width, .1), vertical / em_height
    origin = q[0] + v * font.ascender
    # PyMuPDF applies morph in PDF's upward Y coordinates.
    matrix = fitz.Matrix(float(u[0]), float(-u[1]), float(-v[0]), float(v[1]), 0, 0)
    page.insert_text(fitz.Point(origin), observation['text'], fontsize=1, fontname=fontname,
                     render_mode=3, morph=(fitz.Point(origin), matrix), overlay=True)


def separate_paths(page, observations, page_number):
    """Only fully enclosed, glyph-sized paths qualify; crossing vectors survive."""
    accepted = [o for o in observations if o['page'] == page_number and o['accepted']]
    polygons = [Polygon(o['quad_pt']) for o in accepted]
    if not polygons:
        return [], []
    tree = STRtree(polygons)
    paths = page.get_drawings()
    straight = [(i, np.array([[p['items'][0][1].x,p['items'][0][1].y], [p['items'][0][2].x,p['items'][0][2].y]]))
                for i,p in enumerate(paths) if len(p['items']) == 1 and p['items'][0][0] == 'l']
    from shapely.geometry import LineString
    line_tree = STRtree([LineString(points) for _,points in straight])
    excluded, ambiguous = [], []
    for index, path in enumerate(paths):
        rect = path['rect']
        bounds = shape_box(rect.x0-.01, rect.y0-.01, rect.x1+.01, rect.y1+.01)
        for hit in tree.query(bounds):
            observation, poly = accepted[int(hit)], polygons[int(hit)]
            q = np.asarray(observation['quad_pt'])
            height = min(np.linalg.norm(q[1]-q[0]), np.linalg.norm(q[3]-q[0]))
            kind, shape = geometry(plain(path['items']))
            if kind in {'circle', 'circular_arc'}:
                continue
            points = []
            for item in path['items']:
                for value in item[1:]:
                    if isinstance(value, fitz.Point):
                        points.append([value.x, value.y])
                    elif isinstance(value, fitz.Rect):
                        points.extend([[value.x0, value.y0], [value.x1, value.y1]])
                    elif isinstance(value, fitz.Quad):
                        points.extend([[p.x, p.y] for p in value])
            if not points:
                continue
            # Bounding rectangles of rotated text contain unrelated geometry.
            # Test every original command point against the oriented polygon.
            from shapely.geometry import MultiPoint
            inside = poly.buffer(max(.15, height*.06)).covers(MultiPoint(points))
            extent = max(rect.width, rect.height)
            if not inside or extent > height*1.9:
                continue
            # A long straight line may be an extension/dimension line. Keep it.
            simple_long = kind == 'line' and extent > height*1.15
            connected_to_structure = False
            if kind == 'line':
                a,b = np.asarray(points[0]),np.asarray(points[-1]); length=np.linalg.norm(b-a)
                if length > .01:
                    u=(b-a)/length; normal=np.array([-u[1],u[0]])
                    for peer in line_tree.query(bounds.buffer(.4)):
                        peer_index, ends = straight[int(peer)]
                        if peer_index == index or np.linalg.norm(ends[1]-ends[0]) < height*2:
                            continue
                        perpendicular = np.abs((ends-a)@normal)
                        start,end = sorted((ends-a)@u)
                        if max(perpendicular) < .25 and end >= -.4 and start <= length+.4:
                            connected_to_structure = True
                            break
            entry = {'source_path_index': index, 'ocr_id': observation['id'], 'bbox_pt': list(rect),
                     'confidence': observation['confidence'], 'basis': 'whole original path inside oriented OCR text polygon and glyph-sized'}
            if simple_long or connected_to_structure or observation['conflicting_readings']:
                entry['retention_reason'] = 'connected_to_long_structure' if connected_to_structure else 'long_straight_or_conflicting_text'
                ambiguous.append(entry)
            else:
                excluded.append(entry)
            break
    return excluded, ambiguous


def recover_text(source, destination, settings, engine=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source_hash = file_digest(source)
    cache = destination / 'tiles'
    cache.mkdir(exist_ok=True)
    config = settings.model_dump(mode='json')
    identity = {'source_sha256': source_hash, 'settings': config, 'adapter_version': VERSION}
    previous = destination / 'cache-key.json'
    if previous.exists() and read_json(previous) != identity:
        raise ValueError('OCR 缓存的原图或参数已改变，请使用新的输出目录。')
    write_json(previous, identity)
    if engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError('需要本地 OCR 依赖：python -m pip install -e ".[ocr]"。图纸不会上传。') from exc
        engine = RapidOCR(intra_op_num_threads=settings.threads, inter_op_num_threads=1,
                          det_limit_side_len=settings.tile_pixels, max_side_len=settings.tile_pixels+100)
    observations, page_checks, separations, pending = [], [], [], []
    derived = destination / 'analysis.pdf'
    with fitz.open(source) as doc:
        before = [vector_digest(p) for p in doc]
        for n, page in enumerate(doc, 1):
            if page.get_text().strip() or not page.get_drawings():
                continue
            if page.rotation:
                raise ValueError('OCR 输入须先固化 PDF 页面旋转。')
            values = recognize(page, n, engine, settings, cache)
            cells, grids = recognize_cells(page, n, engine, settings, cache)
            retain_cell_alternatives(cells, values)
            def inside_grid(word):
                b = word['bbox_pt']; x, y = (b[0]+b[2])/2, (b[1]+b[3])/2
                return any(g['bbox_pt'][0] <= x <= g['bbox_pt'][2] and g['bbox_pt'][1] <= y <= g['bbox_pt'][3] for g in grids)
            observations.extend([w for w in values if not inside_grid(w)] + cells)
        observations = deduplicate(observations)
        for word in observations:
            word['accepted'] = word['confidence'] >= settings.min_confidence
            word['needs_review'] = word['confidence'] < settings.review_confidence or bool(word['conflicting_readings'])
        for n, page in enumerate(doc, 1):
            removed, uncertain = separate_paths(page, observations, n)
            separations.extend(dict(p, page=n) for p in removed)
            pending.extend(dict(p, page=n) for p in uncertain)
            for word in observations:
                if word['page'] == n and word['accepted']:
                    insert_hidden_text(page, word)
        doc.save(derived, garbage=3, deflate=True)
    with fitz.open(source) as original, fitz.open(derived) as analysis:
        for n, (a, b) in enumerate(zip(original, analysis), 1):
            scale = min(1., 1800 / max(a.rect.width, a.rect.height))
            matrix = fitz.Matrix(scale, scale)
            same_pixels = a.get_pixmap(matrix=matrix, alpha=False).samples == b.get_pixmap(matrix=matrix, alpha=False).samples
            page_checks.append({'page': n, 'original_vector_sha256': before[n-1],
                                'analysis_vector_sha256': vector_digest(b),
                                'all_vector_geometry_unchanged': before[n-1] == vector_digest(b),
                                'visible_pixels_unchanged': same_pixels,
                                'original_paths': len(a.get_drawings()), 'analysis_paths': len(b.get_drawings()),
                                'recovered_text_characters': len(b.get_text().strip())})
    report = {**identity, 'schema_version': '1.0', 'status': 'needs_review',
              'engine': 'rapidocr_onnxruntime', 'local_only': True,
              'analysis_source': str(derived), 'analysis_source_sha256': file_digest(derived),
              'source_bytes_unchanged': file_digest(source) == source_hash,
              'pages': page_checks, 'words': observations, 'text_strokes': separations, 'ambiguous_paths': pending,
              'counts': {'observations': len(observations), 'accepted': sum(o['accepted'] for o in observations),
                         'review_words': sum(o['needs_review'] for o in observations),
                         'text_strokes': len(separations), 'ambiguous_paths': len(pending)},
              'limitations': ['OCR 结果为识别候选，置信度不等于字符正确率。',
                              '完整原生路径保留；文字区域内无法确定的路径仍参与结构分析并列入疑点。']}
    write_json(destination / 'report.json', report)
    if not report['source_bytes_unchanged'] or not all(p['all_vector_geometry_unchanged'] and p['visible_pixels_unchanged'] for p in page_checks):
        raise ValueError('OCR 分析副本改变了原有矢量或可见图面，已停止后续建模。')
    print(f'[ocr] complete: {report["counts"]}; original vectors and visible pixels unchanged', flush=True)
    return report


def analysis_source(manifest):
    """Validate the complete original -> OCR -> analysis provenance chain."""
    if file_digest(manifest['source']) != manifest['source_sha256']:
        raise ValueError('Source PDF changed; regenerate inputs')
    source = manifest.get('analysis_source', manifest['source'])
    if manifest.get('analysis_source'):
        if file_digest(source) != manifest['analysis_source_sha256']:
            raise ValueError('OCR 分析副本已变更，需重新生成下游结果。')
        if file_digest(manifest['ocr_report']) != manifest['ocr_report_sha256']:
            raise ValueError('OCR 证据报告已变更，需重新生成下游结果。')
    return source


def excluded_indices(report, page_number):
    return {p['source_path_index'] for p in report.get('text_strokes', []) if p['page'] == page_number}


def filtered_paths(page, report, page_number):
    excluded = excluded_indices(report or {}, page_number)
    heights = [min(np.linalg.norm(np.asarray(o['quad_pt'][1])-o['quad_pt'][0]),
                   np.linalg.norm(np.asarray(o['quad_pt'][3])-o['quad_pt'][0]))
               for o in (report or {}).get('words', []) if o['page'] == page_number and o['accepted']]
    # Table finding must also ignore unrecognized small glyph fragments: they
    # otherwise introduce spurious cell edges. Original path records stay intact.
    minimum_grid_extent = max(8., min(28., float(np.median(heights))*2.5)) if heights else 0
    return [p for i, p in enumerate(page.get_drawings()) if i not in excluded
            and max(p['rect'].width,p['rect'].height) > minimum_grid_extent]


def ocr_table_options(page, report, page_number):
    # The default 3 pt snap can pull a precise CAD border toward a distant
    # structural line at a similar X coordinate. Use the same tolerance in
    # whole-page extraction and independently cropped table verification.
    return {'paths': filtered_paths(page, report, page_number), 'snap_tolerance': .5}


def annotate_primitives(primitives, report, page_number):
    exclusions = {p['source_path_index']: p for p in report['text_strokes'] if p['page'] == page_number}
    words = [o for o in report['words'] if o['page'] == page_number and o['accepted']]
    tree = STRtree([Polygon(o['quad_pt']) for o in words]) if words else None
    for p in primitives:
        if p.get('source_path_index') in exclusions:
            proof = exclusions[p['source_path_index']]
            p.update(role='ocr_text_stroke', role_basis=proof['basis'], ocr_evidence=proof,
                     excluded_from_structure=True)
        elif p['primitive_type'] == 'text' and tree is not None:
            candidates = [words[int(i)] for i in tree.query(shape_box(*p['bbox_pt']))]
            if candidates:
                word = min(candidates, key=lambda o: math.dist([(o['bbox_pt'][0]+o['bbox_pt'][2])/2, (o['bbox_pt'][1]+o['bbox_pt'][3])/2],
                                                              [(p['bbox_pt'][0]+p['bbox_pt'][2])/2, (p['bbox_pt'][1]+p['bbox_pt'][3])/2]))
                p.update(text_source='ocr', ocr_id=word['id'], ocr_confidence=word['confidence'],
                         needs_review=word['needs_review'], role_basis='local OCR invisible text layer; inspect original glyphs before accepting numbers')


def svg_path(path):
    commands, previous = [], None
    for item in path['items']:
        op = item[0]
        if op in {'l', 'c'}:
            start = item[1]
            if previous is None or abs(start-previous) > .001:
                commands.append(f'M{start.x:g},{start.y:g}')
            if op == 'l':
                commands.append(f'L{item[2].x:g},{item[2].y:g}')
            else:
                commands.append('C' + ' '.join(f'{p.x:g},{p.y:g}' for p in item[2:]))
            previous = item[-1]
        elif op in {'re', 'qu'}:
            q = fitz.Quad(item[1]) if op == 'qu' else fitz.Quad(item[1].tl, item[1].tr, item[1].bl, item[1].br)
            commands.append(f'M{q.ul.x:g},{q.ul.y:g} L{q.ur.x:g},{q.ur.y:g} L{q.lr.x:g},{q.lr.y:g} L{q.ll.x:g},{q.ll.y:g} Z')
            previous = None
    if path.get('closePath'):
        commands.append('Z')
    fill = '#202020' if 'f' in path['type'] else 'none'
    stroke = '#202020' if 's' in path['type'] else 'none'
    dash = re.search(r'\[([^]]*)\]', path.get('dashes') or '')
    dash_attr = f' stroke-dasharray="{dash[1]}"' if dash and dash[1].strip() else ''
    return f'<path d="{" ".join(commands)}" fill="{fill}" stroke="{stroke}" stroke-width="{path.get("width") or .15:g}"{dash_attr}/>'


def publish_review(source, report, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    pages = []
    with fitz.open(source) as doc:
        for n, page in enumerate(doc, 1):
            scale = min(2., 4000/max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(destination / f'p{n}.png')
            excluded = excluded_indices(report, n)
            layers = {'geometry': [], 'glyphs': []}
            for index, path in enumerate(page.get_drawings()):
                layers['glyphs' if index in excluded else 'geometry'].append(svg_path(path))
            for name, paths in layers.items():
                svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {page.rect.width:g} {page.rect.height:g}">' + ''.join(paths) + '</svg>'
                (destination / f'p{n}-{name}.svg').write_text(svg, encoding='utf-8')
            pages.append({'page': n, 'width': page.rect.width, 'height': page.rect.height})
    write_json(destination / 'report.json', {**report, 'display_pages': pages})
    shutil.copy2(report['analysis_source'], destination / 'analysis.pdf')
    shutil.copytree(Path(__file__).with_name('ocr_web'), destination, dirs_exist_ok=True)
