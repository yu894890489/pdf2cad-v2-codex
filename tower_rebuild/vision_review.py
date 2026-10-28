"""Optional image-based cross-check of native observations. Never grants approval."""
import json
from pathlib import Path
from typing import Literal

import fitz
import httpx
from pydantic import Field

from .io import file_digest, read_json, write_json
from .schema import Evidence, Strict
from .vlm import VlmClient


class VisionFinding(Strict):
    category: Literal['bom', 'member', 'plate', 'hole', 'topology', 'dimension', 'view']
    severity: Literal['conflict', 'uncertain']
    object_ids: list[str] = Field(default_factory=list)
    observation: str = Field(min_length=1)
    suggested_check: str = Field(min_length=1)
    evidence: Evidence


class ViewReview(Strict):
    findings: list[VisionFinding] = Field(default_factory=list)
    unreadable_regions: list[Evidence] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)


def review_with_vision(run_dir, options, client_factory=VlmClient):
    run = Path(run_dir); site = run / 'site'; destination = site / 'vision-review'
    manifest = read_json(run / 'manifest.json')
    names = ['topology/model-preview.json', 'topology/views.json', 'tables/bom.json', 'audit/report.json']
    report = {'schema_version': '1.0', 'source_sha256': manifest['source_sha256'],
              'status': 'disabled' if not options.enabled else 'running', 'views': [], 'errors': [],
              'input_files': {name: file_digest(site / name) for name in names},
              'engineering_accuracy_verified': False,
              'note': '视觉复核仅提出带原页位置的疑点；不修改原生几何、BOM、连接或原初审结论。'}
    if not options.enabled:
        write_json(destination / 'report.json', report)
        return report
    if file_digest(manifest['source']) != manifest['source_sha256']:
        raise ValueError('Source PDF changed before vision review')
    models = read_json(site / names[0]); views = read_json(site / names[1]); bom = read_json(site / names[2])
    audit = read_json(site / names[3]); client = None
    try:
        client = client_factory(options.service, run / 'vision-cache')
        with fitz.open(manifest['source']) as pdf:
            for view in views[:options.max_views]:
                vid = view['id']; print(f'[vision-review] {vid}', flush=True)
                record = {'view_id': vid, 'title': view['title'], 'status': 'pending'}
                stop_service = False
                try:
                    page = pdf[view['page'] - 1]; box = fitz.Rect(view['bbox_pt']) & page.rect
                    if box.is_empty:
                        raise ValueError('Empty original-page view crop')
                    crop = run / 'vision-crops' / f'{vid}.png'; crop.parent.mkdir(parents=True, exist_ok=True)
                    scale = min(6, options.service.image_max_side / max(box.width, box.height))
                    page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=box, alpha=False).save(crop)
                    model = next(v for v in models['views'] if v['view_id'] == vid)
                    known = {o['id'] for kind in ('members', 'plates', 'holes') for o in model[kind]}
                    context = {'original_page_size_pt': [page.rect.width, page.rect.height],
                               'crop_bbox_pt': list(box), 'view': view, 'model': model, 'bom': bom,
                               'initial_findings': [f for f in audit['findings'] if f['view_id'] == vid]}
                    prompt = ('逐项对照当前原图裁剪与原生识别结果：圈号查 BOM；Q345L 角钢，Q345- 板材；'
                              '不把虚线、尺寸线作为杆件。核查杆端被板遮挡的补段、板多边形、孔位、交叉误连、尺寸和视向。'
                              '只记录能定位的冲突和不确定项，空 findings 不代表完整或正确。object_ids 只引用提供的 ID，'
                              '漏检对象没有 ID 时返回空数组。evidence.page 必须为本页，bbox 使用原整页归一坐标，'
                              '不能用裁剪图坐标。无需推断真实孔数或批准模型。数据：' + json.dumps(context, ensure_ascii=False))
                    result = client.ask(f'workflow-review-{vid}', ViewReview, prompt, [crop])
                    for finding in result.findings:
                        if set(finding.object_ids) - known:
                            raise ValueError('Vision review references an unknown observation ID')
                    for evidence in [f.evidence for f in result.findings] + result.unreadable_regions:
                        if evidence.page != view['page']:
                            raise ValueError('Vision evidence references a different page')
                        rect = fitz.Rect(evidence.bbox[0] * page.rect.width, evidence.bbox[1] * page.rect.height,
                                         evidence.bbox[2] * page.rect.width, evidence.bbox[3] * page.rect.height)
                        if not box.intersects(rect):
                            raise ValueError('Vision evidence lies outside the supplied view crop')
                    record.update(status='reviewed_advisory', **result.model_dump(mode='json'))
                except Exception as exc:
                    record.update(status='unavailable', error=f'{type(exc).__name__}: {exc}')
                    report['errors'].append({'view_id': vid, 'message': record['error']})
                    stop_service = isinstance(exc, httpx.TransportError) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 404, 429, 502, 503, 504})
                report['views'].append(record)
                write_json(destination / 'report.json', report)
                if stop_service:
                    break
        reviewed_ids = {v['view_id'] for v in report['views']}
        for view in views:
            if view['id'] in reviewed_ids:
                continue
            report['views'].append({'view_id': view['id'], 'title': view['title'], 'status': 'not_reviewed',
                                    'reason': '视觉服务不可用或本次面数达到配置上限。'})
        report['status'] = 'partial' if report['errors'] or len(views) > options.max_views else 'completed_advisory'
    except Exception as exc:
        report.update(status='unavailable')
        report['errors'].append({'message': f'{type(exc).__name__}: {exc}'})
    finally:
        if client is not None:
            client.close()
    report['counts'] = {'views_total': len(views), 'views_reviewed': sum(v['status'] == 'reviewed_advisory' for v in report['views']),
                        'findings': sum(len(v.get('findings', [])) for v in report['views'])}
    write_json(destination / 'report.json', report)
    return report
