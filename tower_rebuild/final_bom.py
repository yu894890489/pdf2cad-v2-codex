"""Original drawing BOM joined to measurements of the current effective model.

Views are observations, not physical quantities. Never average conflicting
observations, replace stock lengths, or use cached pre-correction measurements.
"""
from collections import defaultdict
import copy
import csv
from datetime import datetime, timezone
import html
import io
import math
from pathlib import Path

from shapely.geometry import MultiPoint

from .io import digest, file_digest, read_json, write_json
from .materials import classify
from .native_bom import compact, split_cell_rows


FILES = {'index.html', 'final-bom.json', 'final-bom.csv', 'model-components.csv'}
COLUMNS = ['图纸', '件号', '规格', '原图长度 (mm)', '原图数量', '原图单重 (kg)',
           '原图总重 (kg)', '原图备注', '模型长度口径', '最终模型长度 (mm)',
           '最大绝对长度差 (mm)', '模型观察数', '修正观察数', '关注原因', '是否需要人工关注']
DETAIL_COLUMNS = ['图纸', '件号', '结构面', '组件 ID', '模型长度口径', '原图长度 (mm)',
                  '修正前模型长度 (mm)', '最终模型长度 (mm)', '长度差 (mm)',
                  '补长 (mm)', '截短 (mm)', '经过修正', '关注原因', '是否需要人工关注']


def load_original_tables(run):
    """Keep the raw table evidence separate from the model input signature.

    Extra fields are joined only after all four normalized BOM fields match.
    A hash records the exact extraction used; it is not an engineering approval.
    """
    path = Path(run) / 'site/tables/tables.json'
    if not path.is_file():
        return {}
    return {'original_tables': read_json(path), 'original_tables_sha256': file_digest(path)}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def original_rows(tables):
    rows = defaultdict(list)
    for table in tables:
        if table.get('kind') != 'bom':
            continue
        for index, cells in enumerate(table.get('rows', [])):
            if len(cells) < 4:
                continue
            try:
                split = split_cell_rows(*cells[:4])
            except ValueError:
                continue
            for position, (mark, spec, length, quantity) in enumerate(split):
                extras, ambiguous = [], False
                for value in (list(cells[4:7]) + [None] * 3)[:3]:
                    if len(split) == 1:
                        extras.append(value)
                    elif not value:
                        extras.append(value)
                    else:
                        lines = value.splitlines()
                        if len(lines) == len(split):
                            extras.append(lines[position])
                        else:
                            extras.append(None)
                            ambiguous = True
                rows[mark].append({'table_id': table.get('id'), 'page': table.get('page'),
                    'row_index': index, 'cells': copy.deepcopy(cells), 'specification': spec,
                    'length_mm': number(length), 'quantity': number(quantity),
                    'unit_weight_kg': extras[0], 'total_weight_kg': extras[1], 'remarks': extras[2],
                    'extras_ambiguous': ambiguous})
    return rows


def measure(face, item, kind):
    if face.get('units') != 'mm':
        return None
    try:
        if kind == 'angle':
            points = [item['a'], item['b']]
            if not all(len(p) == 3 and all(number(v) is not None for v in p) for p in points):
                return None
            length = math.dist(*points)
        else:
            points = item.get('polygon') or []
            if len(points) < 3 or not all(len(p) == 3 and all(number(v) is not None for v in p) for p in points):
                return None
            # Face-local plates lie in XY; rigid world placement changes no length.
            if max(p[2] for p in points) - min(p[2] for p in points) > 1e-6:
                return None
            rectangle = MultiPoint([p[:2] for p in points]).minimum_rotated_rectangle
            if rectangle.geom_type != 'Polygon':
                return None
            corners = list(rectangle.exterior.coords)
            length = max(math.dist(a, b) for a, b in zip(corners, corners[1:]))
        return length if math.isfinite(length) and length > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def build_final_bom(sources, snapshot, tolerance_mm=3.0):
    """Read-only, deterministic measurement of the effective face geometry."""
    if number(tolerance_mm) is None or tolerance_mm < 0:
        raise ValueError('长度差诊断阈值必须是非负有限数。')
    scene = snapshot.get('scene', {})
    faces = snapshot.get('faces', [])
    edge_faces = {e['id']: f['id'] for f in faces for e in f.get('edges', [])}
    joined = {edge_faces[e] for link in scene.get('links', []) for e in (link['a'], link['b']) if e in edge_faces}
    invalid_faces = {edge_faces[e] for link in snapshot.get('assessment', {}).get('links', []) if not link.get('valid')
                     for e in (link['a'], link['b']) if e in edge_faces}
    observed = defaultdict(list)
    for face in faces:
        for kind, field in [('angle', 'members'), ('plate', 'plates')]:
            for item in face.get(field, []):
                length = measure(face, item, kind)
                correction = item.get('correction') or {}
                schematic = item.get('schematic_correction') or {}
                corrected = bool(correction or schematic or (kind == 'plate' and item.get('polygon')
                                                             and face['id'] in scene.get('face_corrections', {})))
                reasons = []
                if face.get('units') != 'mm':
                    reasons.append('结构面尚无毫米比例，不能输出毫米长度')
                elif length is None:
                    reasons.append('杆件端点无效' if kind == 'angle' else '未识别有效板形多边形')
                if kind == 'angle':
                    if not item.get('endpoints_verified'):
                        reasons.append('杆件端点及端套/搭接基准未确认')
                else:
                    reasons.append('板形包络长边与原图下料长口径不同，未直接作差')
                    if not (item.get('outline') or {}).get('engineering_verified'):
                        reasons.append('板形轮廓待核对')
                if corrected:
                    reasons.append('反算或单线图修正候选待确认')
                if face['id'] not in joined:
                    reasons.append('所在结构面尚未连接到其他面')
                if face['id'] in invalid_faces:
                    reasons.append('所在结构面有接缝未通过装配检查')
                for issue in face.get('source_issues', []):
                    if issue.get('object_id') == item['id'] or str(issue.get('mark')) == str(item.get('mark')):
                        if issue.get('kind') != 'member_length_or_projection':
                            reasons.append(issue.get('message') or issue.get('kind', '原图识别待核对'))
                before = number(correction.get('original_length_mm'))
                if before is None and schematic:
                    axis = schematic.get('original_axis_pt')
                    scale = number((face.get('scale') or {}).get('mm_per_pt'))
                    if axis and scale:
                        before = math.dist(axis['a'], axis['b']) * scale
                observed[(face['source_id'], str(item.get('mark', '')))].append({
                    'face_id': face['id'], 'view_id': face.get('view_id'), 'face_title': face.get('title', face['id']),
                    'component_id': item['id'], 'kind': kind, 'length_basis': '杆件轴线' if kind == 'angle' else '板形包络长边',
                    'model_length_mm': length, 'original_model_length_mm': before if corrected else length,
                    'corrected': corrected, 'added_mm': number(correction.get('added_mm')),
                    'trimmed_mm': number(correction.get('trimmed_mm')), 'reasons': list(dict.fromkeys(reasons))})
    rows, source_info = [], []
    from .schematic_audit import assembly_checks
    for source in sources:
        raw = original_rows(source.get('original_tables', []))
        source_rows = copy.deepcopy(source.get('bom', []))
        original_marks = {str(b['mark']) for b in source_rows}
        for sid, mark in observed:
            if sid == source['id'] and mark not in original_marks:
                source_rows.append({'mark': mark, 'specification': '', 'length_mm': None, 'quantity': None})
        checks = assembly_checks(source, faces) if source.get('schematic') else []
        source_info.append({k: copy.deepcopy(source.get(k)) for k in
            ('id', 'name', 'source_sha256', 'source_url', 'result_url', 'original_tables_sha256', 'original_tables')})
        for bom in source_rows:
            mark = str(bom['mark'])
            items = copy.deepcopy(observed.get((source['id'], mark), []))
            reasons = []
            evidence = raw.get(mark, [])
            matches = [r for r in evidence if compact(r['specification']) == compact(bom.get('specification'))
                       and r['length_mm'] == number(bom.get('length_mm')) and r['quantity'] == number(bom.get('quantity'))
                       and (not bom.get('table_id') or r['table_id'] == bom['table_id'])]
            extras = matches[0] if len(matches) == 1 else {}
            if not extras or extras.get('extras_ambiguous'):
                reasons.append('原表行或单重/总重/备注未唯一对应，请核对原表')
            if mark not in original_marks:
                reasons.append('模型件号在原图 BOM 中不存在')
            if sum(str(b['mark']) == mark for b in source_rows) > 1:
                reasons.append('同一图纸 BOM 件号重复，未合并原表数量')
            if not items:
                reasons.append('最终模型中未找到对应组件')
            kind = (bom.get('classification') or classify(bom.get('specification', '')))['kind']
            original = number(bom.get('length_mm'))
            lengths, differences = [], []
            for item in items:
                length = item['model_length_mm']
                item['difference_mm'] = length - original if length is not None and original is not None and kind == item['kind'] == 'angle' else None
                if kind != item['kind']:
                    item['reasons'].append('模型组件类型与 BOM 规格分类不一致')
                if length is not None:
                    lengths.append(length)
                if item['difference_mm'] is not None:
                    differences.append(item['difference_mm'])
                    if abs(item['difference_mm']) > tolerance_mm:
                        item['reasons'].append(f'模型与原图长度差超过 {tolerance_mm:g} mm 诊断阈值')
                reasons.extend(item['reasons'])
            if lengths and max(lengths) - min(lengths) > tolerance_mm:
                reasons.append('同件号的多个模型观察长度不一致，未取平均值')
            if number(bom.get('quantity')) and number(bom['quantity']) > 1:
                reasons.append('原图有多件，跨视图观察尚未映射到唯一实体，未核定模型数量')
            for check in checks:
                if mark in [str(m) for m in check.get('marks', [])] and check.get('status') not in {'reference_only', 'within_diagnostic_bound'}:
                    reasons.append('关联单线图尺寸链尚未通过或对应基准待确认')
            reasons = list(dict.fromkeys(reasons))
            row = {'source_id': source['id'], 'source_name': source.get('name', source['id']), 'mark': mark,
                'original': bom, 'original_table_matches': matches, 'original_table_evidence': evidence,
                'unit_weight_kg': extras.get('unit_weight_kg'), 'total_weight_kg': extras.get('total_weight_kg'),
                'remarks': extras.get('remarks'), 'kind': kind, 'observations': items,
                'model_lengths_mm': sorted(set(round(v, 6) for v in lengths)),
                'max_abs_difference_mm': max(map(abs, differences)) if differences else None,
                'observation_count': len(items), 'corrected_observation_count': sum(i['corrected'] for i in items),
                'needs_attention': bool(reasons), 'attention_reasons': reasons}
            rows.append(row)
    rows.sort(key=lambda r: (r['source_name'], (0, int(r['mark'])) if r['mark'].isdigit() else (1, r['mark'])))
    warnings = [s.get('name', s['id']) + '：尚无可用的最终模型/BOM，未计入本表'
                for s in snapshot.get('sources', []) if s['id'] not in {v['id'] for v in sources}]
    return {'schema_version': '1.0', 'title': snapshot.get('title', '最终模型 BOM'),
        'project_id': snapshot.get('id'), 'revision': snapshot.get('revision'),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'basis_signature': digest({'faces': faces, 'scene': scene, 'sources': source_info,
                                   'bom': [s.get('bom', []) for s in sources], 'checks': [s.get('schematic') for s in sources]}),
        'warnings': warnings,
        'tolerance_mm': tolerance_mm, 'engineering_verified': False,
        'measurement_note': '杆件按最终局部三维端点量取轴线长；刚性摆放不改变长度。板件只量取当前板形的最小包络长边，不能等同下料长。',
        'quantity_note': '原图数量保持不变；多个面上的观察不等于实体数量，未自动求和。',
        'counts': {'rows': len(rows), 'attention': sum(r['needs_attention'] for r in rows),
                   'without_observation': sum(not r['observations'] for r in rows),
                   'without_measurable_length': sum(not r['model_lengths_mm'] for r in rows),
                   'corrected_observations': sum(r['corrected_observation_count'] for r in rows)},
        'sources': source_info, 'rows': rows}


def fmt(value):
    return '' if value is None else f'{value:.3f}'.rstrip('0').rstrip('.') if isinstance(value, float) else str(value)


def summary_values(row):
    return [row['source_name'], row['mark'], row['original'].get('specification'), row['original'].get('length_mm'),
        row['original'].get('quantity'), row['unit_weight_kg'], row['total_weight_kg'], row['remarks'],
        ' / '.join(dict.fromkeys(i['length_basis'] for i in row['observations'])),
        ' / '.join(fmt(v) for v in row['model_lengths_mm']), row['max_abs_difference_mm'],
        row['observation_count'], row['corrected_observation_count'], '；'.join(row['attention_reasons']),
        '是' if row['needs_attention'] else '否']


def safe_csv(value):
    text = fmt(value)
    # CSV quoting alone does not stop spreadsheet formula execution.
    return "'" + text if isinstance(value, str) and text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else text


def csv_text(columns, rows):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(columns)
    writer.writerows([safe_csv(value) for value in row] for row in rows)
    return buffer.getvalue()


def atomic_text(path, text, encoding):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding=encoding)
    temporary.replace(path)


def publish_final_bom(report, destination, parts_url=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / 'final-bom.json', report)
    values = [summary_values(r) for r in report['rows']]
    atomic_text(destination / 'final-bom.csv', csv_text(COLUMNS, values), 'utf-8-sig')
    details = []
    for row in report['rows']:
        for item in row['observations']:
            details.append([row['source_name'], row['mark'], item['face_title'], item['component_id'], item['length_basis'],
                row['original'].get('length_mm'), item['original_model_length_mm'], item['model_length_mm'], item['difference_mm'],
                item['added_mm'], item['trimmed_mm'], '是' if item['corrected'] else '否',
                '；'.join(row['attention_reasons']), '是' if row['needs_attention'] else '否'])
    atomic_text(destination / 'model-components.csv', csv_text(DETAIL_COLUMNS, details), 'utf-8-sig')
    esc = lambda value: html.escape(fmt(value))
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>最终模型 BOM</title><style>body{font:15px system-ui;margin:24px;color:#173957;background:#f7f9fc}h1{font-size:26px}a{color:#175bab;margin-right:20px}table{border-collapse:collapse;background:white;width:100%}td,th{border:1px solid #cbd5df;padding:9px;vertical-align:top}th{background:#234b70;color:white;position:sticky;top:0;white-space:nowrap}td{min-width:75px}td:first-child{min-width:160px;overflow-wrap:anywhere}td:nth-last-child(2){min-width:300px}td:last-child{font-weight:bold}tr[data-attention="1"] td:last-child{background:#fff1d6;color:#864a00}.scroll{overflow:auto;max-height:70vh}input{padding:9px;margin:8px}details{margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}caption{text-align:left;margin:12px 0}</style>',
        '<style>#bom td:nth-child(2){position:sticky;left:0;background:#eff5fb;z-index:1}#bom th:nth-child(2){left:0;z-index:3}#bom td:last-child{position:sticky;right:0;background:white;z-index:1}#bom th:last-child{right:0;z-index:3}</style>',
        '<h1>最终模型 BOM</h1><p>' + esc(report['title']) + ' · 修订 ' + esc(report['revision']) + '</p>',
        '<p><a href="'+esc(parts_url)+'">按模型生成零件放样 DWG</a></p>' if parts_url else '',
        '<nav><a href="final-bom.csv" download>下载 BOM（CSV，可用 Excel 打开）</a><a href="model-components.csv" download>下载逐组件长度明细</a><a href="final-bom.json" download>完整数据及原表</a></nav>',
        '<p>' + esc(report['measurement_note']) + '</p><p>' + esc(report['quantity_note']) + '</p>',
        '<p>长度差超过 ' + esc(report['tolerance_mm']) + ' mm 列为待关注，此值为诊断阈值。反算候选、缺失组件、端点未确认及同件号长度冲突均保留关注原因。</p>',
        '<p>共 ' + str(report['counts']['rows']) + ' 行，需人工关注 ' + str(report['counts']['attention']) + ' 行。生成于 ' + esc(report['generated_at']) + '。重新打开本页可读取最新已保存模型。</p>',
        ''.join('<p role="alert">' + esc(w) + '</p>' for w in report['warnings']),
        '<label>查找件号或规格 <input id="search" type="search" placeholder="件号、规格、图纸"></label><label><input id="attention" type="checkbox">只看需要人工关注</label><span id="visible-count" role="status"></span>',
        '<div class="scroll"><table id="bom"><thead><tr>' + ''.join('<th>' + esc(v) + '</th>' for v in COLUMNS) + '</tr></thead><tbody>']
    for row, cells in zip(report['rows'], values):
        body.append('<tr data-attention="' + str(int(row['needs_attention'])) + '">' + ''.join('<td>' + esc(v) + '</td>' for v in cells) + '</tr>')
    body.append('</tbody></table></div><details><summary>逐组件模型长度（同件号保留各面观察）</summary><div class="scroll"><table><thead><tr>' + ''.join('<th>' + esc(v) + '</th>' for v in DETAIL_COLUMNS) + '</tr></thead><tbody>')
    body.extend('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in cells) + '</tr>' for cells in details)
    body.append('</tbody></table></div></details><details><summary>原图表格数据</summary>')
    for source in report['sources']:
        body.append('<h2>' + esc(source['name']) + '</h2>')
        for table in source.get('original_tables') or []:
            if table.get('kind') != 'bom':
                continue
            body.append('<div class="scroll"><table><caption>' + esc(table.get('title', table.get('id'))) + '</caption>')
            body.extend('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in cells) + '</tr>' for cells in table.get('rows', []))
            body.append('</table></div>')
    body.append('</details><script>const search=document.querySelector("#search"),attention=document.querySelector("#attention"),rows=[...document.querySelectorAll("#bom tbody tr")];function filter(){let count=0;const text=search.value.trim().toLowerCase();for(const row of rows){const match=/^\\d+$/.test(text)?row.cells[1].textContent===text:[...row.cells].slice(0,3).some(c=>c.textContent.toLowerCase().includes(text));row.hidden=!(match&&(!attention.checked||row.dataset.attention==="1"));if(!row.hidden)count++;}document.querySelector("#visible-count").textContent=` 显示 ${count} 行`;}search.addEventListener("input",filter);attention.addEventListener("change",filter);filter();</script></html>')
    atomic_text(destination / 'index.html', '\n'.join(body), 'utf-8')
    return report


def publish_saved_run_bom(run):
    """Export an existing final snapshot without solving or moving its faces."""
    from .assembly_pipeline import load_run_source
    run = Path(run)
    snapshot = read_json(run / 'site/assembly/assembly.json')
    source = load_run_source(run, name=snapshot.get('title'))
    for name, expected in snapshot['automatic_pipeline'].get('input_files', {}).items():
        root = (run / 'site').resolve()
        path = (root / name.replace('\\', '/')).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path) != expected:
            raise ValueError('保存的装配结果已过期，请重新生成装配后导出 BOM。')
    source.update(load_original_tables(run))
    return publish_final_bom(build_final_bom([source], snapshot), run / 'site/final-bom')
