"""Inspect local requirements and optionally test real image input, without a drawing."""
from importlib import metadata
import platform
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from .schema import Strict
from .vlm import VlmClient
from .workflow_settings import load_workflow_settings


class ProbeAnswer(Strict):
    digits: str = Field(pattern=r'^\d{4}$')


def doctor(config=None, probe_vision=False, client_factory=VlmClient):
    options = load_workflow_settings(config)
    result = {'platform': platform.platform(), 'python': platform.python_version(), 'packages': {},
              'profile_name': options.name, 'vision_enabled': options.vision_review.enabled,
              'remote_vision_enabled': options.vision_review.enabled and options.vision_review.service.allow_remote,
              'gpu': [], 'vision_probe': {'status': 'not_requested'},
              'capabilities': {'vector_pdf_pipeline': True, 'scanned_pdf_native_primitives': False,
                               'per_view_solids': True, 'verified_automatic_spatial_assembly': False}}
    for name in ('PyMuPDF', 'numpy', 'scipy', 'shapely', 'pydantic', 'Pillow', 'httpx'):
        try:
            result['packages'][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result['packages'][name] = None
    try:
        import rapidocr_onnxruntime
        result['capabilities']['outlined_text_ocr'] = True
        result['ocr'] = {'mode': options.ocr.mode, 'engine': 'rapidocr_onnxruntime',
                         'version': metadata.version('rapidocr-onnxruntime'), 'local_only': True}
    except (ImportError, metadata.PackageNotFoundError):
        result['capabilities']['outlined_text_ocr'] = False
        result['ocr'] = {'mode': options.ocr.mode, 'install': 'python -m pip install -e ".[ocr]"'}
    executable = shutil.which('nvidia-smi')
    if executable:
        try:
            check = subprocess.run([executable, '--query-gpu=name,memory.total', '--format=csv,noheader'],
                                   capture_output=True, text=True, timeout=10,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if check.returncode == 0:
                result['gpu'] = check.stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if probe_vision:
        if not options.vision_review.enabled:
            raise ValueError('请先在流程配置中启用 vision_review 并填写支持图像输入的模型。')
        client = None
        try:
            with tempfile.TemporaryDirectory(prefix='tower-vision-probe-') as temporary:
                root = Path(temporary); target = root / 'probe.png'; digits = f'{secrets.randbelow(10000):04d}'
                image = Image.new('RGB', (500, 180), 'white'); draw = ImageDraw.Draw(image)
                try:
                    font = ImageFont.truetype('arial.ttf', 90)
                except OSError:
                    font = ImageFont.load_default(size=90)
                draw.text((45, 35), digits, fill='black', font=font); image.save(target)
                client = client_factory(options.vision_review.service, root / 'cache')
                answer = client.ask('image-capability-probe', ProbeAnswer,
                                    '读取图片中四位数字，包含前导零。只输出 digits 字段。', [target], use_cache=False)
                result['vision_probe'] = {'status': 'passed' if answer.digits == digits else 'failed',
                                           'image_input_tested': True,
                                           'note': '仅验证真实图像输入和结构化输出，不代表工程图纸识别准确率。'}
        except Exception as exc:
            result['vision_probe'] = {'status': 'unavailable', 'message': f'{type(exc).__name__}: {exc}'}
        finally:
            if client is not None:
                client.close()
    result['native_runtime_ready'] = all(result['packages'].values())
    return result
