"""Measure candidate solids and compare them with the unchanged first audit.

Lengths are recomputed from the extrusion endpoints. Node spans are reconstructed
from model reference axes, never substituted for a member's full cut length.
Agreement with a reused calibration or BOM input is not independent validation.
"""
from collections import Counter
import math
from pathlib import Path
import shutil

import numpy as np
from shapely.geometry import Polygon

from .io import digest, file_digest, read_json, write_json


def numeric_check(measured, reference, tolerance_mm, rounding_mm=0):
    if measured is None or reference is None:
        return {'reference_mm': reference, 'delta_mm': None, 'status': 'pending'}
    delta = measured - reference
    bound = tolerance_mm + (rounding_mm or 0)
    return {'reference_mm': reference, 'delta_mm': delta, 'diagnostic_bound_mm': bound,
            'status': 'difference' if abs(delta) > bound + 1e-8 else 'within_diagnostic_bound'}


def axis_length(member, ratio):
    if not ratio or not member.get('axis_pt'):
        return None
    return math.dist(member['axis_pt']['a'], member['axis_pt']['b']) * ratio


def point_mm(point, view):
    b = view['bbox_pt']; s = view['scale']['mm_per_pt']
    return [(point[0] - (b[0] + b[2]) / 2) * s,
            ((b[1] + b[3]) / 2 - point[1]) * s, 0.0]


def common_axis(ids, members):
    """Only combine coincident projected axes, not competing nearby members."""
    records = [members[i] for i in ids if i in members]
    if not records or len(records) != len(set(ids)):
        return None
    a, b = (np.array(records[0]['axis_pt'][key], dtype=float) for key in ('a', 'b'))
    if np.linalg.norm(b - a) < 1e-8:
        return None
    u = (b - a) / np.linalg.norm(b - a)
    for record in records[1:]:
        c, d = (np.array(record['axis_pt'][key], dtype=float) for key in ('a', 'b'))
        if np.linalg.norm(d - c) < 1e-8:
            return None
        v = (d - c) / np.linalg.norm(d - c)
        if abs(u[0] * v[1] - u[1] * v[0]) > 1e-5:
            return None
        if max(abs(u[0] * (p - a)[1] - u[1] * (p - a)[0]) for p in (c, d)) > .1:
            return None
    return a, b


def intersection(first, second):
    a, b = first; c, d = second
    matrix = np.column_stack((b - a, c - d))
    if abs(np.linalg.det(matrix)) < 1e-8 * np.linalg.norm(b - a) * np.linalg.norm(d - c):
        return None
    return a + np.linalg.solve(matrix, c - a)[0] * (b - a)


def polygon_measurements(polygon, members, ratio):
    edges = polygon['edges']
    if polygon.get('association_status') != 'projected_face_candidate' or not ratio or len(edges) < 3:
        return None
    axes = [common_axis(e.get('member_ids', []), members) for e in edges]
    if any(a is None for a in axes):
        return None
    vertices = [intersection(axes[i - 1], axes[i]) for i in range(len(axes))]
    if any(v is None for v in vertices):
        return None
    points = [v.tolist() for v in vertices]
    shape = Polygon(points)
    if not shape.is_valid or shape.area < 1e-6:
        return None
    result = []
    for i, axis in enumerate(axes):
        a, b = vertices[i], vertices[(i + 1) % len(vertices)]
        start, end = axis; d = end - start
        fractions = [float(np.dot(p - start, d) / np.dot(d, d)) for p in (a, b)]
        result.append({'measured_mm': float(np.linalg.norm(b - a) * ratio),
                       'a_pt': a.tolist(), 'b_pt': b.tolist(),
                       'axis_parameter_range': fractions,
                       'uses_axis_extension': min(fractions) < -1e-6 or max(fractions) > 1 + 1e-6})
    return result


def dependency(view, member=None, text_ids=()):
    scale = view['scale']
    calibration_ids = [e['id'] for e in scale.get('evidence', [])]
    return {'scale_basis': scale['basis'], 'calibration_ids': calibration_ids,
            'reference_used_in_scale': sorted(set(text_ids) & set(calibration_ids)),
            'bom_scale_reused': 'bom' in scale['basis'],
            'overlap_status': member.get('overlap_status') if member else None,
            'native_projection_reused': True, 'independent_engineering_validation': False}


def compare_model(model, audit, tolerance_mm=3.0):
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError('Diagnostic tolerance must be finite and nonnegative')
    rows = []; view_results = []
    members_by_id = {m['id']: m for v in model['views'] for m in v['members']}
    views = {v['view_id']: v for v in model['views']}

    def add(view, kind, object_id, label, measured=None, reference=None, rounding=0,
            status=None, note='', member=None, text_ids=(), **extra):
        check = numeric_check(measured, reference, tolerance_mm, rounding)
        row = {'id': f'model_check_{len(rows) + 1:04d}', 'view_id': view['view_id'],
               'view_title': view['title'], 'kind': kind, 'object_id': object_id, 'label': label,
               'measured_mm': measured, **check, 'status': status or check['status'],
               'note': note, 'dependency': dependency(view, member, text_ids), **extra}
        rows.append(row)
        return row

    for view in model['views']:
        ratio = view['scale']['mm_per_pt']
        for m in view['members']:
            length = axis_length(m, ratio)
            add(view, 'member_bom', m['id'], f'杆件 {m["mark"]} · 全长 / BOM', length, m['bom_length_mm'],
                member=m, mark=m['mark'], bom_mm=m['bom_length_mm'],
                geometry={'a_mm': point_mm(m['axis_pt']['a'], view), 'b_mm': point_mm(m['axis_pt']['b'], view)} if ratio else None,
                note='由模型挤出两端重新测长；仍是本面投影模型，端点、倾斜和圈号对应待确认。')
            add(view, 'member_section', m['id'], f'杆件 {m["mark"]} · 角钢截面',
                status='input_reused', member=m, mark=m['mark'], model_section=m['section'],
                note='肢宽和厚度直接取自 BOM；数值一致不能作为独立复核。')
        modeled = {m['id'] for m in view['members']}
        for issue in view.get('issues', []):
            if issue['kind'] in {'member_not_modeled', 'member_transverse_cut'} and issue['object_id'] not in modeled:
                add(view, 'member_bom', issue['object_id'], f'杆件 {issue["mark"]} · 未生成纵向实体',
                    mark=issue['mark'], note=issue['message'])
        for plate in view['plates']:
            outline = plate.get('outline'); dims = None
            if outline and ratio:
                xy = np.array(outline['polygon_pt'])
                u = outline.get('stock_envelope_direction')
                if u is not None:
                    u = np.array(u); u = u / np.linalg.norm(u); n = np.array([-u[1], u[0]])
                    dims = sorted([float(np.ptp(xy @ u) * ratio), float(np.ptp(xy @ n) * ratio)])
                else:
                    corners = list(Polygon(xy).minimum_rotated_rectangle.exterior.coords)
                    dims = sorted(math.dist(corners[i], corners[i + 1]) * ratio for i in (0, 1))
            target = sorted([plate['stock'].get('width_mm') or 0, plate['bom_length_mm']])
            for i, label in enumerate(('短边', '长边')):
                add(view, 'plate_envelope', plate['id'], f'板件 {plate["mark"]} · 包络{label}',
                    dims[i] if dims else None, target[i], status='blank_reference' if dims else 'pending',
                    note='成品多边形包络与 BOM 板坯尺寸仅作参考；板坯不等于成品轮廓，轮廓筛选也使用过 BOM。',
                    mark=plate['mark'], envelope_direction=outline.get('stock_envelope_direction') if outline else None)
            add(view, 'plate_thickness', plate['id'], f'板件 {plate["mark"]} · 厚度',
                plate['stock'].get('thickness_mm') if dims else None, plate['stock'].get('thickness_mm'),
                status='input_reused' if dims else 'pending', mark=plate['mark'], note='板厚直接取自 BOM。')
        for hole in view['holes']:
            add(view, 'hole_diameter', hole['id'], '孔位投影 · 直径',
                hole.get('diameter_mm'), hole.get('diameter_mm'),
                status='input_reused' if hole.get('diameter_mm') and ratio else 'pending',
                geometry={'center_mm': point_mm(hole['center_pt'], view)} if ratio else None,
                text_ids=hole.get('diameter_evidence', []),
                note='明确孔径直接用于生成圆孔或标记；未标注孔径的圆环不是实测孔径。')
        # Neighbour distances are measurements, not inferred matches to nearby text.
        pairs = {}
        for owner in view['members'] + [p for p in view['plates'] if p.get('outline')]:
            holes = [h for h in view['holes'] if owner['id'] in h['member_ids'] + h['plate_ids']]
            for h in holes:
                others = [k for k in holes if k['id'] != h['id'] and math.dist(k['center_pt'], h['center_pt']) > 1e-6]
                if not others:
                    continue
                k = min(others, key=lambda k: math.dist(k['center_pt'], h['center_pt']))
                pair = tuple(sorted([h['id'], k['id']]))
                item = pairs.setdefault(pair, {'holes': [h, k], 'owner_ids': []})
                item['owner_ids'].append(owner['id'])
        for ids, item in pairs.items():
            h, k = item['holes']
            add(view, 'hole_distance', '|'.join(ids), '相邻孔中心距 · 尺寸链待对应',
                math.dist(h['center_pt'], k['center_pt']) * ratio if ratio else None,
                owner_ids=item['owner_ids'], hole_ids=list(ids),
                note='仅量测同一候选零件上的近邻孔。原初审尚无可靠孔距标注对应，保留待定；不据距离最近的数字认定孔距。')

    for check in audit.get('span_bom_checks', []):
        view = views.get(check['view_id'])
        if not view:
            continue
        member = members_by_id.get(check['member_id'])
        measured = axis_length(member, view['scale']['mm_per_pt']) if member else None
        full_length = check.get('computed_cut_length_mm')
        text_ids = [check.get('span_text_id')]
        text_ids += [e['text_id'] for e in check.get('end_basis', {}).get('selected', [])]
        text_ids += [e['text_id'] for e in check.get('span_extensions', {}).get('segments', [])]
        add(view, 'member_initial_audit', check['member_id'], f'杆件 {check["mark"]} · 全长 / 初审换算',
            measured, full_length, check.get('rounding_bound_mm', 0), member=member,
            mark=check['mark'], bom_mm=check.get('bom_length_mm'), initial_check_id=check['id'],
            original_span_mm=check.get('span_mm'), formula=check.get('formula'),
            text_ids=[t for t in text_ids if t], initial_status=check['status'],
            note=('已使用初审的完整端部与分段换算。' if full_length is not None else
                  '初审未完成跨度到全长换算，禁止把该段跨度与整根杆长直接比较。'),
            pending_reasons=check.get('pending_reasons', []))

    for annotation in audit.get('annotations', []):
        view = views.get(annotation['view_id']); member = members_by_id.get(annotation.get('member_id'))
        if not view:
            continue
        unambiguous = annotation.get('association_status') != 'ambiguous' and annotation.get('outline_direction_agrees') is True
        add(view, 'member_annotation', annotation.get('member_id'), f'杆件 {annotation.get("mark")} · 全长 / 杆旁标注',
            axis_length(member, view['scale']['mm_per_pt']) if member and unambiguous else None,
            annotation.get('length_mm'), .5, member=member, text_ids=annotation.get('source_text_ids', []),
            mark=annotation.get('mark'), initial_check_id=annotation['id'], initial_status=annotation['status'],
            bom_mm=annotation.get('bom_length_mm'),
            note='沿用初审的候选对应；圈号和标注仍有疑点时保留原结论。' if unambiguous else '杆旁标注对应含糊或方向不一致，暂不比较。')

    for polygon in audit.get('polygons', []):
        view = views.get(polygon['view_id'])
        if not view:
            continue
        measurements = polygon_measurements(polygon, members_by_id, view['scale']['mm_per_pt'])
        for i, edge in enumerate(polygon['edges']):
            measurement = measurements[i] if measurements else None
            value = measurement['measured_mm'] if measurement else None
            row = add(view, 'node_span', polygon['id'], '尺寸图 · 模型参考轴线交点跨度',
                      value, edge.get('value_mm'), edge.get('rounding_mm', 0),
                      text_ids=[edge.get('text_id')], polygon_id=polygon['id'], edge_index=i,
                      text_id=edge.get('text_id'), mark='/'.join(edge.get('marks', [])),
                      geometry=measurement, initial_status=polygon.get('closure', {}).get('status'),
                      note='由模型参考轴线求交后量测；交点可位于杆端外，不代表实体已经接触。共用投影和比例，不能独立证明原尺寸正确。' if measurement else
                           '结构面对应、模型轴线或比例不足，未把示意小三角形当作实体。')
            for finding in audit.get('findings', []):
                if finding.get('source_text_id') == edge.get('text_id') and finding.get('recommended_mm') is not None:
                    row['initial_finding_id'] = finding['id']
                    row['candidate_check'] = numeric_check(value, finding['recommended_mm'], tolerance_mm, edge.get('rounding_mm', 0))
                    row['candidate_note'] = '初审建议值保留为候选；模型复用原投影和部分尺寸，不据模型吻合自动修订图纸。'

    for view in model['views']:
        selected = [r for r in rows if r['view_id'] == view['view_id']]
        lengths = [r for r in selected if r['kind'] == 'member_bom' and r['measured_mm'] and r['reference_mm']]
        ratios = [r['measured_mm'] / r['reference_mm'] for r in lengths]
        view_results.append({'view_id': view['view_id'], 'title': view['title'], 'scale': view['scale'],
                             'counts': dict(Counter(r['status'] for r in selected)),
                             'member_length_ratio_median': float(np.median(ratios)) if ratios else None,
                             'note': '同面比例系统偏差、杆端缺段及错误件号需结合原图分开核对。'})
    return {'schema_version': '1.0', 'stage': 'model_dimension_audit', 'status': 'needs_review',
            'source_sha256': model['source_sha256'], 'title': audit.get('title', '三维尺寸复核'),
            'engineering_accuracy_verified': False, 'diagnostic_tolerance_mm': tolerance_mm,
            'tolerance_note': f'差异提示阈值为 {tolerance_mm:g} mm 加原标注/换算舍入界限；仅供排查，不是加工或验收公差。',
            'rows': rows, 'views': view_results, 'counts': dict(Counter(r['status'] for r in rows)),
            'kind_counts': dict(Counter(r['kind'] for r in rows)), 'initial_findings': audit.get('findings', []),
            'limitations': ['逐面投影实体尚未完成空间装配；不量测预览用的前后层间距和角钢转角。',
                            '数值接近仅表示当前模型与该依据相容，复用的 BOM、比例和投影不构成独立证据。',
                            '板坯包络与成品板形不同；板厚、角钢截面和明确孔径为输入复用。',
                            '孔距标注与孔中心的尺寸链尚未建立；所列近邻距离保留待复核。',
                            '不自动改写原 PDF、BOM、初审判断或模型尺寸。']}


def validate_inputs(run, model, audit):
    manifest = read_json(run / 'manifest.json'); source = manifest['source_sha256']
    from .ocr_input import analysis_source
    if file_digest(manifest['source']) != source or model['source_sha256'] != source or audit['source_sha256'] != source:
        raise ValueError('Source mismatch; regenerate topology, audit and preview')
    analysis_source(manifest)
    site = run / 'site'
    if any(file_digest(site / name) != value for name, value in model['input_files'].items()):
        raise ValueError('Preview inputs changed; regenerate audit and preview before comparing')
    names = ['topology/report.json', 'topology/views.json', 'topology/members.json', 'topology/geometry.json',
             'tables/bom.json', 'structure/primitives.json', 'structure/callouts.json']
    if audit['input_sha256'] != digest({str(Path(name)): file_digest(site / name) for name in names}):
        raise ValueError('Initial dimension audit is stale; regenerate it before comparing')


def audit_model_dimensions(run_dir, tolerance_mm=3.0):
    run = Path(run_dir); site = run / 'site'; target = site / 'model-audit'
    model = read_json(site / 'topology/model-preview.json'); audit = read_json(site / 'audit/report.json')
    validate_inputs(run, model, audit)
    report = compare_model(model, audit, tolerance_mm)
    report['input_files'] = {name: file_digest(site / name) for name in ('topology/model-preview.json', 'audit/report.json')}
    write_json(target / 'report.json', report)
    shutil.copytree(Path(__file__).parent / 'model_audit_web', target, dirs_exist_ok=True)
    # Install measurement API without changing generated model data or geometry.
    shutil.copy2(Path(__file__).parent / 'topology_web/model-layer.js', site / 'topology/model-layer.js')
    for folder in ('topology', 'audit'):
        index = site / folder / 'index.html'
        if index.exists():
            html = index.read_text(encoding='utf-8')
            if 'id="model-audit-link"' not in html:
                html = html.replace('</header>', '<a id="model-audit-link" href="../model-audit/">三维尺寸复核</a></header>', 1)
                index.write_text(html, encoding='utf-8')
    lines = ['# 三维尺寸与原初审比对', '', report['tolerance_note'], '',
             '单位：mm；当前模型保持原状，原初审保持原结论。', '',
             '| 结构面 / 项目 | 模型测量 | 原初审 / BOM | 差值 | 状态 |',
             '| --- | ---: | ---: | ---: | --- |']
    for r in report['rows']:
        if r['kind'] not in {'member_bom', 'member_initial_audit', 'node_span'} or r['reference_mm'] is None:
            continue
        fmt = lambda value: '待定' if value is None else f'{value:.3f}'
        lines.append(f'| {r["view_title"]} / {r["label"]} / {r.get("mark", "")} | {fmt(r["measured_mm"])} | {fmt(r["reference_mm"])} | {fmt(r["delta_mm"])} | {r["status"]} |')
    lines += ['', '## 范围与依据', '', *['- ' + text for text in report['limitations']]]
    (run / 'model-dimension-audit.md').write_text('\n'.join(lines), encoding='utf-8')
    failure = run / 'failure.json'
    if failure.exists() and read_json(failure).get('stage') == 'model-audit':
        failure.unlink()
    print(f'[model-audit] {report["counts"]}; status=needs_review', flush=True)
    return report
