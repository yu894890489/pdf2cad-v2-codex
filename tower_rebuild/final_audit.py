"""Unified, evidence-linked final checklist for every uploaded drawing."""
from collections import Counter
from pathlib import Path
import shutil

from .io import file_digest, read_json, write_json
from .model_dimension_audit import validate_inputs
from .workflow_settings import load_workflow_settings


def final_check(run_dir):
    run = Path(run_dir); site = run / 'site'
    names = {'summary': 'summary.json', 'bom': 'tables/bom.json', 'bom_check': 'tables/bom-check.json',
             'topology': 'topology/report.json', 'members': 'topology/members.json', 'connections': 'topology/connections.json',
             'inventory': 'topology/final-check.json', 'model': 'topology/model-preview.json',
             'audit': 'audit/report.json', 'comparison': 'model-audit/report.json'}
    data = {key: read_json(site / name) for key, name in names.items()}
    validate_inputs(run, data['model'], data['audit'])
    comparison = data['comparison']
    source = data['model']['source_sha256']
    if comparison['source_sha256'] != source or any(file_digest(site / n) != value for n, value in comparison['input_files'].items()):
        raise ValueError('三维尺寸比对已过期；先重新运行 model-audit。')
    options = load_workflow_settings(run / 'workflow-config.json' if (run / 'workflow-config.json').exists() else None)
    state = read_json(run / 'workflow.json') if (run / 'workflow.json').exists() else {}
    vision_path = site / 'vision-review/report.json'
    vision = read_json(vision_path) if vision_path.exists() else {'status': 'disabled' if not options.vision_review.enabled else 'unavailable'}
    if vision_path.exists():
        names['vision'] = 'vision-review/report.json'
        if vision.get('source_sha256') != source or any(file_digest(site / n) != value for n, value in vision.get('input_files', {}).items()):
            vision = {'status': 'stale', 'errors': [{'message': '视觉复核使用旧输入，需要重新执行。'}]}
    inventory = data['inventory']; counts = inventory['counts']; checks = []

    def check(key, title, status, message, link, object_ids=()):
        checks.append({'id': key, 'title': title, 'status': status, 'message': message,
                       'evidence_url': link, 'object_ids': list(object_ids)})

    coverage = state.get('input_coverage')
    if state.get('ocr'):
        ocr = state['ocr']
        check('ocr', 'OCR 文字与矢量笔画分离', 'pending',
              f'恢复 {ocr["counts"]["accepted"]} 条文字，分离 {ocr["counts"]["text_strokes"]} 条文字笔画；'
              f'{ocr["counts"]["review_words"]} 条文字需重点核对。原图及全部矢量路径保留。', '../ocr/')
    check('input', '输入页面覆盖', 'pass' if coverage and not coverage['issues'] else 'pending',
          f'原生内容覆盖待核对页数：{coverage["pages_with_coverage_issues"]}。' if coverage else '旧运行没有逐页输入检查记录。', '../')
    check('bom', 'BOM 原始行与件号唯一性', 'pass' if data['bom_check']['status'] == 'passed' else 'fail',
          f'{len(data["bom"])} 个 BOM 件号；检查重复、缺项和相互矛盾的原始行。', '../tables/bom-check.json')
    check('primitives', '图元保存与表格分区', 'pass' if data['summary']['checks'] and all(data['summary']['checks'].values()) else 'fail',
          '核对原生路径、表格与结构分区、原始行数值和源文件。', '../summary.json')
    missing = inventory['missing_member_marks'] + inventory['missing_plate_marks']
    check('marks', '杆件和板件件号覆盖', 'fail' if missing else 'pass',
          '未找到件号：' + ('、'.join(missing) or '无；件号覆盖不等于实物数量完整。'), '../topology/final-check.json', missing)
    uncertain_members = [m['id'] for m in data['members'] if m['status'] != 'reviewed_2d_association']
    check('associations', '圈号、杆件轮廓与杆端', 'pending' if uncertain_members or counts['member_incomplete'] else 'pass',
          f'{len(uncertain_members)} 条轮廓对应尚未确认，{counts["member_incomplete"]} 条杆端未完整。', '../topology/', uncertain_members)
    connections = [c['id'] for c in data['connections'] if c['status'] == 'needs_review']
    check('connections', '杆件、连接板及接点关系', 'pending' if connections or uncertain_members else 'pass',
          f'{len(connections)} 条连接关系待核对；交叉本身不视为连接。', '../topology/', connections)
    findings = data['audit']['findings']
    incomplete = [s['id'] for s in data['audit']['span_bom_checks'] if s.get('computed_cut_length_mm') is None]
    check('initial_audit', '尺寸示意图与下料长度初审', 'fail' if findings else 'pending' if incomplete else 'pass',
          f'{len(findings)} 项初审疑点，{len(incomplete)} 条跨度尚缺完整端部换算依据。', '../audit/', [f['id'] for f in findings])
    length_rows = [r for r in comparison['rows'] if r['kind'] in {'member_bom', 'member_initial_audit', 'node_span'}]
    differences = [r['id'] for r in length_rows if r['status'] == 'difference']
    pending = sum(r['status'] == 'pending' for r in length_rows)
    check('model_dimensions', '图纸与模型几何尺寸终检', 'fail' if differences else 'pending' if pending else 'pass',
          f'{len(differences)} 条差异记录、{pending} 条对应待定。比例和 BOM 复用不计为独立验证。', '../model-audit/', differences)
    check('plates', '连接板多边形', 'pending' if counts['plate_shapes_pending'] else 'pass',
          f'{counts["plate_polygons"]} 个板形候选，{counts["plate_shapes_pending"]} 条轮廓尚未确定。', '../topology/')
    check('holes', '孔形、孔径、孔距与归属', 'pending',
          f'{counts["hole_projections"]} 个孔/螺栓投影，{counts["explicit_hole_diameters"]} 个明确孔径，{counts["hole_owner_pending"]} 个归属待定；孔距尺寸链仍需对应。', '../model-audit/')
    check('quantities', '实物数量与跨面去重', 'pending',
          '同件号可对应多根实物，同一根实物可出现在多个视图；目前观察数不能用于 BOM 实物数量验收。', '../topology/graph.json')
    check('assembly', '各结构面的空间装配', 'pending',
          '当前输出逐面三维实体；跨面刚体变换、共享实物和角钢朝向尚未全部确认。', '../topology/')
    visual_findings = sum(len(v.get('findings', [])) for v in vision.get('views', []))
    check('vision', '可选视觉模型交叉复核', 'not_requested' if not options.vision_review.enabled else 'pending',
          f'服务状态：{vision["status"]}；发现 {visual_findings} 条视觉疑点。空疑点列表不能批准模型。',
          '../vision-review/report.json' if vision_path.exists() else '../')
    actual_counts = {'angle_models': sum(len(v['members']) for v in data['model']['views']),
                     'plate_polygons': sum(bool(p.get('outline')) for v in data['model']['views'] for p in v['plates']),
                     'hole_projections': sum(len(v['holes']) for v in data['model']['views'])}
    inventory_matches = all(actual_counts[key] == counts[key] for key in actual_counts)
    check('model_inventory', '模型实体与识别清单对应', 'pass' if inventory_matches else 'fail',
          f'模型实际记录：角钢 {actual_counts["angle_models"]}，板形 {actual_counts["plate_polygons"]}，孔位 {actual_counts["hole_projections"]}；'
          + ('与清单数量一致。' if inventory_matches else '与识别清单不一致，需重新生成预览。'), '../topology/model-preview.json')
    generated = actual_counts['angle_models'] > 0 or actual_counts['plate_polygons'] > 0
    check('generation', '候选三维生成', 'pass' if generated else 'fail',
          f'{counts["angle_models"]} 条角钢观察实体、{counts["plate_polygons"]} 个板实体；均保留来源和待定条件。', '../topology/')
    report = {'schema_version': '1.0', 'stage': 'final_check', 'source_sha256': source,
              'title': data['summary']['title'], 'status': 'needs_review' if generated else 'no_model',
              'model_generated': generated, 'model_counts': actual_counts, 'engineering_accuracy_verified': False,
              'checks': checks, 'counts': dict(Counter(c['status'] for c in checks)),
              'input_files': {name: file_digest(site / name) for name in names.values()},
              'requirements': ['提供清晰、未加密、包含可读 BOM 和原生线条的 PDF。扫描图需要 OCR、矢量化和对应精度验证。',
                               'BOM 需有唯一件号、规格、数量和杆长；独立图纸重复编号应分开上传。',
                               '图中需能确定杆件端部、尺寸基准及视图对应；信息不足时需补充详图或人工复核。',
                               '视觉模型为可选辅助。其输出不覆盖几何证据、数量核对和尺寸计算。']}
    write_json(site / 'final/report.json', report)
    shutil.copytree(Path(__file__).parent / 'final_web', site / 'final', dirs_exist_ok=True)
    for folder in ('topology', 'audit', 'model-audit'):
        index = site / folder / 'index.html'
        html = index.read_text(encoding='utf-8')
        if 'id="final-check-link"' not in html:
            index.write_text(html.replace('</header>', '<a id="final-check-link" href="../final/">全流程终检</a></header>', 1), encoding='utf-8')
    labels = {'pass': '本项检查通过', 'fail': '存在差异', 'pending': '待核对', 'not_requested': '未启用'}
    lines = ['# 全流程终检', '', f'候选模型生成：{generated}；工程准确性尚未确认。', '',
             '| 检查项目 | 结果 | 说明 |', '| --- | --- | --- |']
    lines += [f'| {c["title"]} | {labels[c["status"]]} | {c["message"]} |' for c in checks]
    lines += ['', '## 新图纸条件', '', *['- ' + s for s in report['requirements']]]
    (run / 'final-check.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'[final-check] {report["counts"]}; model_generated={generated}; status={report["status"]}', flush=True)
    return report
