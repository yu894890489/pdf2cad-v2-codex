"""One local CAD detail per model part variant, with verified DWG conversion."""
from datetime import datetime, timezone
import html
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

import ezdxf
from shapely.geometry import Point, Polygon

from .final_bom import number
from .io import digest, read_json, write_json
from .materials import classify

FORMAT_VERSION = '2'


def model_basis(snapshot):
    # Rigid placement does not change a part drawing. Corrections/scales do.
    return digest([FORMAT_VERSION, snapshot.get('input_digest'), snapshot.get('faces', [])])


def safe_name(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value)).strip('._')[:65] or 'part'


def clean_text(value):
    return str(value).replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}').replace('\n', ' ').replace('\r', ' ')


def find_accoreconsole():
    configured = os.environ.get('TOWER_ACCORECONSOLE')
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file() or candidate.name.lower() != 'accoreconsole.exe':
            raise ValueError('TOWER_ACCORECONSOLE 须指向有效的 accoreconsole.exe。')
        return candidate
    found = shutil.which('accoreconsole.exe')
    if found:
        return Path(found)
    if os.name != 'nt':
        return None
    import winreg
    candidates = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as child:
                                location = winreg.QueryValueEx(child, 'InstallLocation')[0]
                                candidate = Path(location) / 'accoreconsole.exe'
                                if candidate.is_file():
                                    candidates.append(candidate)
                        except OSError:
                            continue
            except OSError:
                continue
    return sorted(set(candidates), key=str, reverse=True)[0] if candidates else None


def normalized_plate(item):
    points = item.get('polygon') or []
    if len(points) < 3 or any(len(p) != 3 or any(number(v) is None for v in p) for p in points):
        raise ValueError('板件缺少有效模型轮廓')
    if max(p[2] for p in points) - min(p[2] for p in points) > 1e-6:
        raise ValueError('板件不是本面平面轮廓，不能直接生成平面放样')
    poly = Polygon([p[:2] for p in points])
    if not poly.is_valid or poly.area < 1e-8:
        raise ValueError('模型板形自交或退化，需先修正轮廓')
    rectangle = list(poly.minimum_rotated_rectangle.exterior.coords)
    a, b = max(zip(rectangle, rectangle[1:]), key=lambda pair: math.dist(*pair))
    length = math.dist(a, b)
    u = [(b[i] - a[i]) / length for i in range(2)]
    if u[0] < -1e-8 or (abs(u[0]) < 1e-8 and u[1] < 0):
        u = [-v for v in u]
    v = [-u[1], u[0]]
    def rotate(p):
        return [sum(p[i] * axis[i] for i in range(2)) for axis in (u, v)]
    rotated = [rotate(p) for p in points]
    origin = [min(p[i] for p in rotated) for i in range(2)]
    def local(p):
        return [rotate(p)[i] - origin[i] for i in range(2)]
    ring = [local(p) for p in points]
    if math.dist(ring[0], ring[-1]) < 1e-8:
        ring.pop()
    # Winding/start vertex are not different parts; never reflect the contour.
    choices = [ring[i:] + ring[:i] for i in range(len(ring))]
    backward = list(reversed(ring))
    choices += [backward[i:] + backward[:i] for i in range(len(ring))]
    ring = min(choices, key=lambda r: [[round(x, 6) for x in p] for p in r])
    shape = Polygon(ring)
    holes, rejected = [], []
    for hole in item.get('drill_holes_local', []):
        diameter = number(hole.get('diameter_mm'))
        center = hole.get('center')
        if not diameter or diameter <= 0 or not center or not hole.get('evidence'):
            rejected.append(hole.get('id', '未知孔'))
            continue
        center = local(center)
        if not shape.buffer(-diameter / 2).covers(Point(center)):
            rejected.append(hole.get('id', '未知孔'))
            continue
        holes.append({'center': center, 'diameter_mm': diameter, 'evidence': hole['evidence']})
    holes.sort(key=lambda h: (*h['center'], h['diameter_mm']))
    return ring, holes, rejected


def build_parts(snapshot, bom):
    lookup = {(f['id'], m['id']): (f, m) for f in snapshot['faces'] for field in ('members', 'plates') for m in f.get(field, [])}
    grouped, omitted = {}, []
    for row in bom['rows']:
        if not row['observations']:
            omitted.append({'source_name': row['source_name'], 'mark': row['mark'], 'reason': '最终模型中没有组件观察'})
        for observation in row['observations']:
            reference = {'source_id': row['source_id'], 'source_name': row['source_name'], 'mark': row['mark'],
                'face_id': observation['face_id'], 'face_title': observation['face_title'], 'component_id': observation['component_id'],
                'corrected': observation['corrected'], 'original_model_length_mm': observation['original_model_length_mm']}
            face, item = lookup[observation['face_id'], observation['component_id']]
            try:
                if face.get('units') != 'mm' or observation['model_length_mm'] is None:
                    raise ValueError('无可量取的毫米模型几何，未用 BOM 尺寸替代生成')
                section = classify(row['original'].get('specification', ''))
                kind = section['kind']
                if kind != observation['kind'] or kind not in {'angle', 'plate'}:
                    raise ValueError('模型类型与 BOM 规格不符或尚不支持此种截面')
                reasons = list(row['attention_reasons'])
                geometry = {'kind': kind, 'length_mm': observation['model_length_mm']}
                if kind == 'angle':
                    geometry.update(legs_mm=section['legs_mm'], thickness_mm=section['thickness_mm'])
                    reasons.append('两肢为当前模型正投影，端部切角、火曲和角钢转角尚未建模；不是带折弯余量的板材展开图')
                    if item.get('hole_ids'):
                        reasons.append('杆件孔尚未对应到具体肢面及孔径，未绘制为可加工孔')
                    else:
                        reasons.append('模型未提供杆件钻孔数据；未画孔不代表无孔')
                else:
                    thickness = number(section.get('thickness_mm'))
                    if not thickness or thickness <= 0:
                        raise ValueError('板件厚度未确定')
                    ring, holes, rejected = normalized_plate(item)
                    geometry.update(polygon=ring, holes=holes, thickness_mm=thickness,
                                    length_mm=max(p[0] for p in ring), width_mm=max(p[1] for p in ring))
                    if rejected:
                        reasons.append('部分模型孔缺标注或不再完整落入修正板形，未输出为加工孔')
                    reasons.append('板形按当前模型导出，火曲、坡口及未确认孔位需按原图复核')
                signature = digest([row['source_id'], row['mark'], section['normalized'],
                                    _rounded(geometry), row['original'].get('length_mm'), row.get('remarks')])[:16]
                if signature not in grouped:
                    filename = f"P{safe_name(row['mark'])}_{safe_name(section['normalized'])}_L{geometry['length_mm']:.3f}_{signature[:8]}"
                    grouped[signature] = {'id': signature, 'filename': filename, 'kind': kind,
                        'mark': row['mark'], 'specification': row['original'].get('specification'),
                        'source_name': row['source_name'], 'source_id': row['source_id'],
                        'bom_length_mm': row['original'].get('length_mm'), 'bom_quantity': row['original'].get('quantity'),
                        'remarks': row.get('remarks'), 'geometry': geometry, 'observations': [],
                        'needs_attention': True, 'attention_reasons': [], 'dwg_status': 'pending'}
                group = grouped[signature]
                group['observations'].append(reference)
                group['attention_reasons'] = list(dict.fromkeys(group['attention_reasons'] + reasons))
            except ValueError as exc:
                omitted.append({**reference, 'reason': str(exc)})
    return list(grouped.values()), omitted


def _rounded(value):
    if isinstance(value, float): return round(value, 6)
    if isinstance(value, dict): return {k: _rounded(v) for k, v in value.items() if k != 'evidence'}
    if isinstance(value, list): return [_rounded(v) for v in value]
    return value


def write_part_dxf(part, path):
    geometry = part['geometry']; length = geometry['length_mm']
    text_size = max(3., length / 180)
    gap = max(30., text_size * 5)
    doc = ezdxf.new('R2010'); doc.units = 4
    doc.header['$INSUNITS'] = 4; doc.header['$MEASUREMENT'] = 1
    doc.header['$LUNITS'] = 2; doc.header['$LUPREC'] = 3
    doc.styles.new('PART_TEXT', dxfattribs={'font': 'simhei.ttf'})
    for layer, color in [('PART_OUTLINE', 2), ('PART_SECTION', 2), ('HOLES', 5), ('DIMENSIONS', 7),
                         ('LABELS', 7), ('REVIEW', 1), ('REFERENCE', 8)]:
        doc.layers.new(layer, dxfattribs={'color': color})
    doc.appids.new('TOWER_PART')
    space = doc.modelspace()
    def text(value, point, layer='LABELS', size=None, width=None):
        return space.add_mtext(clean_text(value), dxfattribs={'insert': point, 'char_height': size or text_size,
            'width': width or max(length, 300), 'style': 'PART_TEXT', 'layer': layer})
    def poly(points, layer='PART_OUTLINE'):
        entity = space.add_lwpolyline(points, close=True, dxfattribs={'layer': layer})
        entity.set_xdata('TOWER_PART', [(1000, part['id']), (1000, part['mark']), (1000, layer)])
        return entity
    def dimension(a, b, base, angle=0):
        dim = space.add_linear_dim(base=base, p1=a, p2=b, angle=angle, dxfattribs={'layer': 'DIMENSIONS'},
            override={'dimtxt': text_size, 'dimasz': text_size * .5, 'dimexo': text_size * .3,
                      'dimexe': text_size * .4, 'dimdec': 3, 'dimzin': 8, 'dimtad': 1, 'dimtxsty': 'PART_TEXT'})
        dim.render()
    if geometry['kind'] == 'angle':
        a, b = geometry['legs_mm']; thickness = geometry['thickness_mm']
        top = a
        poly([(0, 0), (length, 0), (length, a), (0, a)])
        y = -gap - b
        poly([(0, y), (length, y), (length, y+b), (0, y+b)])
        dimension((0, a), (length, a), (0, a+gap))
        dimension((0, 0), (0, a), (-gap, 0), 90)
        dimension((0, y), (0, y+b), (-gap, y), 90)
        text('肢 A 侧视图', (0, -text_size))
        text('肢 B 侧视图', (0, y-text_size))
        x = length + gap * 3
        poly([(x, 0), (x+a, 0), (x+a, thickness), (x+thickness, thickness), (x+thickness, b), (x, b)], 'PART_SECTION')
        dimension((x, 0), (x+a, 0), (x, -gap))
        dimension((x, 0), (x, b), (x-gap, 0), 90)
        text(f'L 形截面 t={thickness:g} mm（内外圆角未建模）', (x, -gap*2), width=max(gap*8, 220))
        lower = y - gap * 2
        extra = []
    else:
        ring = geometry['polygon']; top = geometry['width_mm']; thickness = geometry['thickness_mm']
        poly(ring)
        dimension((0, top), (length, top), (0, top+gap))
        dimension((0, 0), (0, top), (-gap, 0), 90)
        y = -gap * 2 - thickness
        poly([(0, y), (length, y), (length, y+thickness), (0, y+thickness)], 'PART_SECTION')
        dimension((length, y), (length, y+thickness), (length+gap, y), 90)
        text('板厚侧视图', (0, y-text_size))
        for i, hole in enumerate(geometry['holes'], 1):
            entity = space.add_circle(hole['center'], hole['diameter_mm']/2, dxfattribs={'layer': 'HOLES'})
            entity.set_xdata('TOWER_PART', [(1000, part['id']), (1000, f'H{i}')])
            text(f'H{i}', (hole['center'][0]+text_size, hole['center'][1]+text_size), size=text_size*.7, width=gap*2)
        for i, point in enumerate(ring, 1):
            text(f'V{i}', (point[0]+text_size*.5, point[1]+text_size*.5), layer='REFERENCE', size=text_size*.65, width=gap*2)
        lower = y - gap * 2
        extra = ['板形顶点坐标：以本图左下包络角为原点，X/Y 单位 mm']
        extra += [f'V{i}:  X={p[0]:.3f}  Y={p[1]:.3f}' for i, p in enumerate(ring, 1)]
        extra += [f'H{i}: X={h["center"][0]:.3f}  Y={h["center"][1]:.3f}  直径={h["diameter_mm"]:.3f}' for i,h in enumerate(geometry['holes'],1)]
    title = f'件号 {part["mark"]}  {part["specification"]}  模型长度 {length:.3f} mm'
    text(title, (0, top+gap*4), size=text_size*1.35, width=max(length+gap*8, 500))
    info = [f'模型零件放样候选 · 几何 1:1 · 单位 mm · DWG 2018',
        f'原图 BOM 长度 {part["bom_length_mm"]} mm；原图数量 {part["bom_quantity"]}（未分配到本长度变体）',
        f'模型观察 {len(part["observations"])} 处；图纸：{part["source_name"]}',
        '来源结构面：' + ' / '.join(dict.fromkeys(o['face_title'] for o in part['observations'])),
        '是否需要人工关注：是', '原图备注：' + (part.get('remarks') or '无'),
        *part['attention_reasons'], *extra]
    for line in info:
        text(line, (0, lower), layer='REVIEW' if line in part['attention_reasons'] or '人工关注' in line else 'LABELS', width=max(length, 600))
        # Reserve enough vertical space for long Chinese notes at small parts.
        estimated = sum(1 if ord(ch)>255 else .55 for ch in line) * text_size
        lower -= text_size * 1.7 * max(1, math.ceil(estimated/max(length,600)))
    doc.set_modelspace_vport(height=max(top-lower+gap*5, 300), center=(length/2, (top+gap*5+lower)/2))
    doc.saveas(path)
    audit = ezdxf.readfile(path).audit()
    if audit.has_errors:
        raise ValueError('DXF 自检发现错误，未转换此零件。')
    return doc


def geometry_signature(doc):
    result = []
    for entity in doc.modelspace():
        layer = entity.dxf.layer
        if layer not in {'PART_OUTLINE', 'PART_SECTION', 'HOLES'}:
            continue
        if entity.dxftype() == 'LWPOLYLINE':
            result.append([layer, 'LWPOLYLINE', bool(entity.closed), [list(v) for v in entity.get_points('xy')]])
        elif entity.dxftype() == 'CIRCLE':
            result.append([layer, 'CIRCLE', list(entity.dxf.center), entity.dxf.radius])
        else:
            raise ValueError('DWG 回读后出现不支持的零件几何类型')
    return sorted(_rounded(result), key=str)


def write_preview(doc, path):
    from ezdxf.addons.drawing import RenderContext, Frontend, svg, layout
    backend = svg.SVGBackend()
    Frontend(RenderContext(doc), backend).draw_layout(doc.modelspace())
    # A fixed preview sheet avoids unbounded SVG height in the renderer's
    # single-auto-dimension mode. CAD modelspace geometry remains 1:1.
    path.write_text(backend.get_string(layout.Page(420, 297, margins=layout.Margins(10, 10, 10, 10))), encoding='utf8')


def run_console(executable, input_file, commands, folder, log, timeout=60):
    script = folder/'operation.scr'
    # All command arguments are generated paths, never source drawing text.
    script.write_text('_.FILEDIA\n0\n_.CMDDIA\n0\n' + commands + '\n_.QUIT\n', encoding='utf-8')
    profile = folder/'profile'; profile.mkdir(exist_ok=True)
    try:
        result = subprocess.run([str(executable), '/i', str(input_file.resolve()), '/s', str(script.resolve()), '/l', 'en-US',
                                 '/isolate', 'TowerParts', str(profile.resolve())],
            cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        log.write_bytes(result.stdout)
    except subprocess.TimeoutExpired as exc:
        log.write_bytes(exc.stdout or b'')
        raise ValueError('AutoCAD 转换超时，未将该文件标记为成功') from exc
    if result.returncode:
        raise ValueError('AutoCAD 转换进程异常，请查看本地转换日志')


def quote_path(path):
    value = str(path.resolve()).replace('\\', '/')
    if any(c in value for c in '"\r\n'):
        raise ValueError('CAD 输出目录不能包含引号或换行')
    return '"' + value + '"'


def convert_and_verify(dxf_path, dwg_path, executable):
    source = ezdxf.readfile(dxf_path)
    expected = geometry_signature(source)
    def annotations(doc):
        texts = sorted(e.plain_text() for e in doc.modelspace().query('MTEXT'))
        dimensions = sorted(round(float(e.get_measurement()), 6) for e in doc.modelspace().query('DIMENSION'))
        return texts, dimensions
    # Autodesk's profile helper may briefly retain the working directory after
    # the core process exits. Cleanup must not turn a verified file into failure.
    with tempfile.TemporaryDirectory(prefix='_cad_', dir=dwg_path.parent, ignore_cleanup_errors=True) as temporary:
        folder = Path(temporary)
        log_dir = dwg_path.parent/'logs'; log_dir.mkdir(exist_ok=True)
        output = folder/'output.dwg'
        run_console(executable, dxf_path, '_.SAVEAS\n2018\n'+quote_path(output), folder, log_dir/(dwg_path.stem+'.convert.log'))
        if not output.is_file() or output.stat().st_size < 128 or output.read_bytes()[:6] != b'AC1032':
            raise ValueError('AutoCAD 未输出有效的 DWG 2018 文件')
        roundtrip = folder/'roundtrip.dxf'
        run_console(executable, output, '_.DXFOUT\n'+quote_path(roundtrip)+'\n16', folder, log_dir/(dwg_path.stem+'.verify.log'))
        if not roundtrip.is_file():
            raise ValueError('DWG 生成后无法回读验证')
        actual = ezdxf.readfile(roundtrip)
        if actual.units != 4 or geometry_signature(actual) != expected or actual.audit().has_errors:
            raise ValueError('DWG 回读后的单位或零件几何不一致')
        if annotations(actual) != annotations(source):
            raise ValueError('DWG 回读后的文字或尺寸标注不一致')
        shutil.copy2(output, dwg_path)
    return {'format': 'AC1032', 'units': 'mm', 'roundtrip_geometry_verified': True, 'annotations_verified': True}


def publish_part_drawings(snapshot, bom, destination, converter=None, progress=None, cancel=None):
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=True)
    parts, omitted = build_parts(snapshot, bom)
    if len(parts) > 1500:
        raise ValueError('零件变体超过 1500 个，请按图纸分批导出。')
    if converter is None:
        converter = find_accoreconsole()
    report = {'schema_version': FORMAT_VERSION, 'project_id': snapshot.get('id'), 'revision': snapshot.get('revision'),
        'basis_signature': model_basis(snapshot), 'title': snapshot.get('title'),
        'generated_at': datetime.now(timezone.utc).isoformat(), 'status': 'running', 'units': 'mm',
        'dwg_version': '2018', 'converter': 'AutoCAD Core Console' if converter else None,
        'grouping': '按图纸、件号、规格、实际长度及轮廓/孔位分组；仅相同模型几何合并观察，不合并原图数量。',
        'attention_scope': '人工关注原因沿用件号级 BOM 汇总，包含该件号其他结构面的未确认观察。',
        'engineering_verified': False, 'parts': parts, 'omitted': omitted, 'completed': 0, 'total': len(parts)}
    for index, part in enumerate(parts):
        if cancel and cancel():
            report['status'] = 'interrupted'; break
        dxf = destination/(part['filename']+'.dxf'); dwg = destination/(part['filename']+'.dwg')
        try:
            doc = write_part_dxf(part, dxf)
            part['dxf'] = dxf.name
            try:
                preview = destination/(part['filename']+'.svg')
                write_preview(doc, preview)
                part['svg'] = preview.name
            except Exception:
                part['preview_error'] = '未生成 SVG 预览，请使用 CAD 文件查看。'
            if converter:
                part['verification'] = convert_and_verify(dxf, dwg, converter)
                part.update(dwg=dwg.name, dwg_status='verified')
            else:
                part.update(dwg_status='needs_converter', error='未找到 AutoCAD Core Console；DXF 已生成，DWG 尚未生成。')
        except Exception as exc:
            part.update(dwg_status='failed', error=str(exc))
        report['completed'] = index+1
        write_json(destination/'report.json', report)
        if progress:
            progress({'completed': index+1, 'total': len(parts), 'current': part['mark'],
                      'verified': sum(p['dwg_status']=='verified' for p in parts)})
    verified = sum(p['dwg_status']=='verified' for p in parts)
    if report['status'] != 'interrupted':
        report['status'] = 'complete' if parts and verified == len(parts) else 'partial' if verified else 'needs_converter' if not converter else 'blocked'
    report['counts'] = {'variants': len(parts), 'verified_dwg': verified, 'omitted_observations': len(omitted),
                        'needs_attention': sum(p['needs_attention'] for p in parts)}
    write_json(destination/'report.json', report)
    write_index(report, destination)
    with zipfile.ZipFile(destination/'parts-dwg.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(destination/'report.json', 'report.json')
        for part in parts:
            if part.get('dwg'):
                archive.write(destination/part['dwg'], part['dwg'])
        archive.writestr('README.txt', '单位 mm；DWG 2018。按规格、最终模型长度及轮廓/孔位分别出图。\n'
            '当前模型存在未确认的端点、孔位、切角和火曲，图内标记需人工关注。未生成项见 report.json。\n'
            '人工关注原因沿用件号级 BOM 汇总，包含该件号其他结构面的未确认观察。\n'
            '数量是原图 BOM 数量，尚未分配到长度变体；不能把观察数当作实物数量。\n')
    return report


def write_index(report, destination):
    esc = lambda v: html.escape(str(v))
    parts = report['parts']
    rows = []
    for part in parts:
        links = ''.join(f'<a href="{esc(part[key])}" download>{key.upper()}</a> ' for key in ('dwg','dxf') if part.get(key))
        if part.get('svg'):
            links += '<a href="'+esc(part['svg'])+'" target="_blank" rel="noopener">预览</a>'
        status = {'verified':'已回读验证', 'failed':'失败', 'needs_converter':'需要 DWG 转换器', 'pending':'尚未转换'}.get(part['dwg_status'],part['dwg_status'])
        rows.append('<tr>' + ''.join('<td>'+esc(v)+'</td>' for v in (part['source_name'], part['mark'], part['specification'],
            f"{part['geometry']['length_mm']:.3f}", len(part['observations']), status))+
            '<td>'+links+'</td><td>是：'+esc('；'.join(part['attention_reasons']) + ('；'+part['error'] if part.get('error') else ''))+'</td></tr>')
    omitted = ''.join('<li>'+esc(o['mark']+'：'+o['reason'])+'</li>' for o in report['omitted'])
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>零件放样 DWG</title><style>body{font:15px system-ui;margin:24px;color:#173957}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd5df;padding:9px;text-align:left}th{background:#234b70;color:white}td:last-child{max-width:600px}a{margin-right:12px}.scroll{overflow:auto}</style>'
    body += '<h1>模型零件放样图</h1><p>'+esc(report['title'])+'</p><p>'+esc(report['grouping'])+'</p>'
    body += '<p>'+esc(report.get('attention_scope','人工关注原因沿用件号级 BOM 汇总，包含该件号其他结构面的未确认观察。'))+'</p>'
    body += f'<p>DWG 已回读验证 {report["counts"]["verified_dwg"]} / {len(parts)} 份；几何单位 mm，模型空间 1:1。当前图为模型候选放样，人工关注原因随图保留。</p>'
    body += '<p><a href="parts-dwg.zip" download>下载全部 DWG</a><a href="report.json">完整清单与未生成原因</a></p><div class="scroll"><table><tr><th>图纸</th><th>件号</th><th>规格</th><th>模型长度 mm</th><th>观察数</th><th>DWG 状态</th><th>文件</th><th>是否需要人工关注</th></tr>'+''.join(rows)+'</table></div>'
    body += '<details><summary>未生成项 '+str(len(report['omitted']))+'</summary><ul>'+omitted+'</ul></details></html>'
    (destination/'index.html').write_text(body, encoding='utf8')
