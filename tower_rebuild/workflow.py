"""One local PDF -> evidence arrays -> checked per-view solid previews."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import time

import fitz

from .io import file_digest, read_json, write_json
from .native_bom import BomIntegrityError
from .workflow_settings import load_workflow_settings


STAGES = [('input', '检查图纸'), ('ocr', '恢复文字与分离文字笔画'), ('decompose', '解析 BOM 与图元'),
          ('topology', '整理结构面与连接关系'), ('audit', '尺寸初审'),
          ('preview', '识别检查与三维预览'), ('model_audit', '三维尺寸与初审比对'),
          ('vision_review', '可选视觉复核'), ('final_check', '全流程终检')]


class UnsupportedDrawing(ValueError):
    def __init__(self, message, *, pages=None, code=None):
        super().__init__(message)
        self.pages = pages
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def inspect_pdf(path, *, allow_outline=False):
    with fitz.open(path) as doc:
        if not doc.is_pdf or doc.needs_pass or not doc.page_count:
            raise UnsupportedDrawing('需要未加密且包含页面的有效 PDF。')
        pages = []
        for page in doc:
            raster_areas = [fitz.Rect(i['bbox']).get_area() / page.rect.get_area() for i in page.get_image_info()]
            pages.append({'page': page.number + 1, 'rotation': page.rotation,
                          'native_paths': len(page.get_drawings()),
                          'text_characters': len(page.get_text().strip()),
                          'raster_images': len(raster_areas),
                          'largest_raster_fraction': max(raster_areas, default=0)})
        if any(p['rotation'] for p in pages):
            raise UnsupportedDrawing('PDF 含页面旋转元数据；请将旋转固化后重新导入，避免图元坐标错位。',
                                     pages=pages, code='page_rotation')
        has_vectors = any(p['native_paths'] for p in pages)
        has_text = any(p['text_characters'] for p in pages)
        has_images = any(p['raster_images'] for p in pages)
        if has_vectors and not has_text and not allow_outline:
            raise UnsupportedDrawing(
                'PDF 有原生矢量路径，但可提取文字为 0；文字可能在 CAD 导出时转成了轮廓或线条。'
                '当前流程无法读取 BOM、件号和尺寸。需重新导出保留可提取文字的 PDF，'
                '或接入 OCR 文字恢复并将文字笔画与结构线条分离；原有结构矢量应保留。',
                pages=pages, code='vector_without_text')
        if not has_vectors:
            if has_images:
                message = ('PDF 含栅格图片，但未检出原生矢量路径；图片中的结构需进行矢量化，'
                           '并对缺失的文字执行 OCR。当前流程尚未支持该输入。')
                code = 'raster_without_vectors'
            elif has_text:
                message = 'PDF 有可提取文字，但未检出原生矢量路径；缺少可用于建模的结构图元。'
                code = 'text_without_vectors'
            else:
                message = 'PDF 未检出原生矢量路径、可提取文字或栅格图片；请检查页面是否为空。'
                code = 'empty_content'
            raise UnsupportedDrawing(message, pages=pages, code=code)
        return pages


def input_coverage(pages):
    issues = []
    for page in pages:
        reasons = []
        if not page['native_paths']:
            reasons.append('未检测到原生矢量路径')
        if not page['text_characters']:
            reasons.append('未检测到可读文字')
        if page.get('largest_raster_fraction', 0) > .5:
            reasons.append('大幅栅格图片中的线条尚未被原生图元流程覆盖')
        if reasons:
            issues.append({'page': page['page'], 'reasons': reasons,
                           'message': f'第 {page["page"]} 页：' + '；'.join(reasons)})
    return {'status': 'needs_review' if issues else 'native_content_available',
            'page_count': len(pages), 'pages_with_coverage_issues': len(issues), 'issues': issues,
            'note': '原生内容可用只说明具备提取条件，不等于已完整识别页面中的所有零件。'}


def write_input_review(out, state):
    assessment = state.get('input_coverage', {})
    bom_path = out / 'site/tables/bom-check.json'
    bom = read_json(bom_path) if bom_path.exists() else None
    if bom:
        state['bom_integrity'] = {k: bom[k] for k in ('status', 'row_observations', 'unique_marks', 'issues')}
    lines = ['# 输入覆盖与 BOM 检查', '', f'文件：{state["source_name"]}', '',
             f'状态：{state["status"]}。原始 PDF 和各行证据保留，未自动修改件号、规格、数量。', '', '## 页面覆盖', '']
    for p in state.get('pages', []):
        lines.append(f'- 第 {p["page"]} 页：原生路径 {p["native_paths"]}，文字字符 {p["text_characters"]}，最大栅格图占页 {p.get("largest_raster_fraction", 0):.1%}。')
    lines.extend('- ' + i['message'] for i in assessment.get('issues', []))
    if state.get('ocr'):
        recovery = state['ocr']; counts = recovery['counts']
        lines.extend(['', '## OCR 文字恢复', '',
                      f'恢复文字 {counts["accepted"]} 条；重点核对 {counts["review_words"]} 条；分离文字笔画 {counts["text_strokes"]} 条。',
                      '原图和全部原生路径保留；分析副本只增加文字层。OCR 候选需要与原图核对。',
                      '交互复核：site/ocr/index.html；完整证据：ocr/report.json。'])
    if bom:
        lines.extend(['', '## BOM 原始行', '',
                      f'原始行观察 {bom["row_observations"]}，不同件号 {bom["unique_marks"]}；检查状态 {bom["status"]}。'])
        for issue in bom['issues']:
            lines.extend(['', '- ' + issue['message']])
            for r in issue['rows']:
                lines.append(f'  - 第 {r["evidence"]["page"]} 页 / 件号 {r["mark"]} / {r["specification"]} / 长度 {r["length_mm"]:g} / 数量 {r["quantity"]} / 原页归一化位置 {r["evidence"]["bbox"]}')
    if state.get('error'):
        lines.extend(['', '## 当前阻碍', '', state['error']['message']])
    (out / 'input-review.md').write_text('\n'.join(lines), encoding='utf-8')


def process_drawing(pdf, out, table_regions=None, review=None, title=None, config=None):
    """New output per run; never mix an old model with a new source or attempt."""
    options = load_workflow_settings(config)
    pdf, out = Path(pdf).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('完整流程需要新的或空的输出目录，请更换 --out；已有结果可用分阶段命令重算。')
    lock = out / '.workflow.lock'
    with lock.open('x', encoding='ascii') as stream:
        stream.write('running')
    state = {'schema_version': '1.0', 'status': 'running', 'created_at': now(),
             'source_name': title or pdf.name, 'source_sha256': None, 'current_stage': None,
             'stages': [{'id': key, 'label': label, 'status': 'pending'} for key, label in STAGES],
             'engineering_accuracy_verified': False, 'local_only': True, 'error': None}
    state['profile_name'] = options.name
    state['remote_vision_enabled'] = options.vision_review.enabled and options.vision_review.service.allow_remote
    state['local_only'] = not state['remote_vision_enabled']
    write_json(out / 'workflow-config.json', options.model_dump(mode='json'))

    def save():
        state['updated_at'] = now()
        write_json(out / 'workflow.json', state)

    def step(key, action):
        stage = next(s for s in state['stages'] if s['id'] == key)
        stage.update(status='running', started_at=now())
        state['current_stage'] = key
        save()
        print(f'[workflow] {key}: {stage["label"]}', flush=True)
        started = time.monotonic()
        value = action()
        stage.update(status='completed', finished_at=now(), duration_seconds=round(time.monotonic() - started, 3))
        save()
        return value

    try:
        def prepare_input():
            if not pdf.is_file() or pdf.suffix.lower() != '.pdf':
                raise ValueError('请选择本机存在的 PDF 文件。')
            source = out / 'input' / 'source.pdf'
            source.parent.mkdir()
            shutil.copy2(pdf, source)
            state['source_sha256'] = file_digest(source)
            state['pages'] = inspect_pdf(source, allow_outline=options.ocr.mode == 'auto')
            state['input_coverage'] = input_coverage(state['pages'])
            state['input_files'] = {}
            for name, path in [('table-regions', table_regions), ('review', review)]:
                if path:
                    target = source.parent / (name + '.json')
                    shutil.copy2(Path(path).resolve(), target)
                    state['input_files'][name] = str(target)
            return source

        source = step('input', prepare_input)
        ocr_report = None
        if options.ocr.mode == 'auto' and any(p['native_paths'] and not p['text_characters'] for p in state['pages']):
            from .ocr_input import recover_text, publish_review
            recovered = step('ocr', lambda: recover_text(source, out / 'ocr', options.ocr))
            ocr_report = out / 'ocr/report.json'
            state['ocr'] = {k: recovered[k] for k in ('status', 'counts', 'source_bytes_unchanged', 'pages')}
            publish_review(source, recovered, out / 'site/ocr')
            save()
        else:
            next(s for s in state['stages'] if s['id'] == 'ocr').update(status='skipped', reason='原生文字可用或 OCR 已关闭')
            save()
        from .decomposition import decompose
        from .topology import topology
        from .dimension_audit import audit_dimensions
        from .model_preview import build_preview
        from .model_dimension_audit import audit_model_dimensions
        from .vision_review import review_with_vision
        from .final_audit import final_check

        summary = step('decompose', lambda: decompose(source, out, state['input_files'].get('table-regions'), ocr_report=ocr_report))
        summary['title'] = Path(state['source_name']).stem
        write_json(out / 'site/summary.json', summary)
        if not summary['bom_rows']:
            raise UnsupportedDrawing('没有解析出 BOM。表格和图元已保留；需补充表格区域或先完成 OCR，暂不生成三维。')
        report = step('topology', lambda: topology(out, state['input_files'].get('review')))
        if not report['counts']['views']:
            raise UnsupportedDrawing('没有分出可核对的结构视图；表格与图元结果已保留。')
        audit = step('audit', lambda: audit_dimensions(out))
        check = step('preview', lambda: build_preview(out))
        comparison = step('model_audit', lambda: audit_model_dimensions(out, options.diagnostic_tolerance_mm))
        state['model_dimension_counts'] = comparison['counts']
        if options.vision_review.enabled:
            vision = step('vision_review', lambda: review_with_vision(out, options.vision_review))
        else:
            vision = review_with_vision(out, options.vision_review)
            next(s for s in state['stages'] if s['id'] == 'vision_review').update(status='skipped', reason='配置未启用视觉复核')
            save()
        state['vision_review_status'] = vision['status']
        final = step('final_check', lambda: final_check(out))
        state['final_check_counts'] = final['counts']
        state.update(status='needs_review', counts=check['counts'],
                     missing_member_marks=check['missing_member_marks'],
                     missing_plate_marks=check['missing_plate_marks'],
                     views=len(check['views']), bom_rows=summary['bom_rows'],
                     dimension_findings=len(check['dimension_findings']),
                     message='候选模型与全流程终检已生成，请查看尺寸差异、识别缺项和待确认的连接关系。')
        if not check['counts']['angle_models'] and not check['counts']['plate_polygons']:
            state['status'] = 'no_model'
            state['message'] = '表格、拓扑和检查报告已生成，但没有足够证据建立实体模型。'
        state['artifacts'] = {'tables': 'site/', 'topology': 'site/topology/',
                              'audit': 'site/audit/', 'model_audit': 'site/model-audit/', 'final': 'site/final/', 'bom': 'site/tables/bom.json',
                              'model': 'site/topology/model-preview.json',
                              'checks': 'site/topology/final-check.json', 'report': 'model-preflight.md'}
        (out / 'failure.json').unlink(missing_ok=True)
    except Exception as exc:
        status = 'unsupported' if isinstance(exc, (UnsupportedDrawing, BomIntegrityError)) else 'failed'
        state.update(status=status, error={'stage': state['current_stage'], 'type': type(exc).__name__, 'message': str(exc)})
        if isinstance(exc, UnsupportedDrawing):
            if exc.pages is not None:
                state['pages'] = exc.pages
                state['input_coverage'] = input_coverage(exc.pages)
            if exc.code:
                state['error']['code'] = exc.code
        for stage in state['stages']:
            if stage['status'] == 'running':
                stage.update(status=status, finished_at=now())
        write_json(out / 'failure.json', state['error'])
        print(f'[workflow] {status}: {exc}', flush=True)
    finally:
        state['finished_at'] = now()
        write_input_review(out, state)
        save()
        lock.unlink(missing_ok=True)
    return state
