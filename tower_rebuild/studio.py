"""Loopback-only upload portal with isolated, sequential drawing jobs."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlsplit, parse_qs
import json
import mimetypes
import secrets
import shutil
import subprocess
import sys
import uuid

from .io import read_json, write_json
from .server import Handler, LocalHTTPServer
from .workflow import now
from .workflow_settings import load_workflow_settings
from .assembly_projects import AssemblyProjects, AssemblyConflict


MAX_UPLOAD = 100 * 1024 * 1024
ACTIVE = {'queued', 'running'}


class JobStore:
    def __init__(self, root, config=None):
        self.config = load_workflow_settings(config)
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='drawing-job')
        self.process = None
        self.closing = False
        self.jobs = {}
        for file in self.root.glob('*/job.json'):
            job = read_json(file)
            if job['id'] != file.parent.name:
                continue
            if job['status'] in ACTIVE:
                job.update(status='interrupted', error='上次服务中断，请点击重试。')
                write_json(file, job)
            self.jobs[job['id']] = job
        self.assemblies = AssemblyProjects(self)

    def save(self, job):
        job['updated_at'] = now()
        write_json(self.root / job['id'] / 'job.json', job)

    def reserve(self, filename):
        with self.lock:
            if self.closing:
                raise ValueError('服务正在关闭。')
            if sum(j['status'] in ACTIVE for j in self.jobs.values()) >= 20:
                raise ValueError('待处理图纸已达 20 张，请等待现有任务完成。')
            jid = uuid.uuid4().hex
            folder = self.root / jid
            folder.mkdir()
            return jid, folder

    def submit(self, jid, filename, retry_of=None):
        job = {'id': jid, 'name': filename, 'status': 'queued', 'created_at': now(), 'retry_of': retry_of}
        with self.lock:
            config_file = self.root / jid / 'workflow-config.json'
            if not config_file.exists():
                write_json(config_file, self.config.model_dump(mode='json'))
            self.jobs[jid] = job
            self.save(job)
            self.executor.submit(self._run, jid)
        return self.snapshot(jid)

    def _run(self, jid):
        folder = self.root / jid
        with self.lock:
            job = self.jobs[jid]
            if self.closing:
                return
            job.update(status='running', error=None)
            self.save(job)
        try:
            with (folder / 'process.log').open('wb') as log:
                with self.lock:
                    if self.closing:
                        return
                    self.process = subprocess.Popen(
                        [sys.executable, '-m', 'tower_rebuild', 'process', str(folder / 'source.pdf'), '--out', str(folder / 'run'), '--title', job['name'], '--config', str(folder / 'workflow-config.json')],
                        stdout=log, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    child = self.process
                code = child.wait()
            with self.lock:
                workflow = read_json(folder / 'run/workflow.json') if (folder / 'run/workflow.json').exists() else {}
                if self.closing:
                    job.update(status='interrupted', error='服务关闭，处理已中断；可以重试。')
                elif code in (0, 2) and workflow.get('status') in {'needs_review', 'no_model', 'unsupported'}:
                    job.update(status=workflow['status'], error=(workflow.get('error') or {}).get('message'))
                else:
                    job.update(status='failed', error=(workflow.get('error') or {}).get('message', f'处理进程退出 {code}，请查看运行日志。'))
                self.save(job)
                if job['status']=='needs_review' and workflow.get('assembly'):
                    related=[r['id'] for r in self.assemblies.records.values() if jid in r['job_ids']]
                    if related:
                        for aid in related:self.assemblies.refresh(aid)
                    else:
                        self.assemblies.create([jid],title='自动装配 · '+job['name'][:100])
        except Exception as exc:
            with self.lock:
                job.update(status='failed', error=str(exc))
                self.save(job)
        finally:
            with self.lock:
                self.process = None

    def retry(self, jid):
        with self.lock:
            old = self.jobs[jid]
            if old['status'] in ACTIVE:
                raise ValueError('该图纸正在处理，请等待完成。')
            new_id, folder = self.reserve(old['name'])
            shutil.copy2(self.root / jid / 'source.pdf', folder / 'source.pdf')
            previous_config = self.root / jid / 'workflow-config.json'
            if previous_config.exists():
                shutil.copy2(previous_config, folder / 'workflow-config.json')
            return self.submit(new_id, old['name'], retry_of=jid)

    def snapshot(self, jid):
        with self.lock:
            job = dict(self.jobs[jid])
            folder = self.root / jid
            workflow = folder / 'run/workflow.json'
            job['workflow'] = read_json(workflow) if workflow.exists() else None
            job['links'] = {'source': f'/api/jobs/{jid}/source'}
            related = [r for r in self.assemblies.records.values() if jid in r['job_ids']]
            if related:
                aid = max(related,key=lambda r:r['created_at'])['id']
                job['links']['assembly'] = f'/assemblies/{aid}/'
            site = folder / 'run/site'
            for key, target in [('ocr', 'ocr/index.html'), ('tables', 'index.html'), ('topology', 'topology/index.html'),
                                ('automaticassembly','assembly/index.html'),
                                ('audit', 'audit/index.html'), ('modelaudit', 'model-audit/index.html'), ('final', 'final/index.html'), ('bom', 'tables/bom.json'),
                                ('bomcheck', 'tables/bom-check.json'), ('bomrows', 'tables/bom-observations.json'),
                                ('model', 'topology/model-preview.json'), ('checks', 'topology/final-check.json')]:
                if (site / target).is_file() and job['status'] not in ACTIVE:
                    job['links'][key] = f'/results/{jid}/' + target.replace('index.html', '')
            if (folder / 'run/model-preflight.md').is_file() and job['status'] not in ACTIVE:
                job['links']['report'] = f'/api/jobs/{jid}/report'
            if (folder / 'run/input-review.md').is_file() and job['status'] not in ACTIVE:
                job['links']['inputreport'] = f'/api/jobs/{jid}/input-report'
            if (folder / 'process.log').is_file():
                job['links']['log'] = f'/api/jobs/{jid}/log'
            return job

    def listing(self):
        with self.lock:
            return [self.snapshot(jid) for jid in sorted(self.jobs, key=lambda k: self.jobs[k]['created_at'], reverse=True)]

    def close(self):
        with self.lock:
            self.closing = True
            for job in self.jobs.values():
                if job['status'] == 'queued':
                    job.update(status='interrupted', error='服务关闭，尚未开始；可以重试。')
                    self.save(job)
            child = self.process
            if child and child.poll() is None:
                child.terminate()
        if child:
            child.wait(timeout=15)
        self.executor.shutdown(wait=True, cancel_futures=True)


class StudioHandler(Handler):
    def __init__(self, *args, store, token, **kwargs):
        self.store, self.token = store, token
        super().__init__(*args, **kwargs)

    def respond_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def send_file(self, file, *, download=False):
        if not file.is_file():
            self.send_error(404)
            return
        self.send_response(200)
        mime = self.extensions_map.get(file.suffix, mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        if file.suffix in {'.log', '.md'}:
            mime = 'text/plain; charset=utf-8'
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(file.stat().st_size))
        if download:
            self.send_header('Content-Disposition', f'attachment; filename="{file.name}"')
        self.end_headers()
        if self.command != 'HEAD':
            with file.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        bits = path.strip('/').split('/')
        try:
            if path == '/api/session':
                vision = self.store.config.vision_review
                self.respond_json({'token': self.token, 'max_upload_bytes': MAX_UPLOAD,
                                   'profile_name': self.store.config.name,
                                   'vision_mode': 'disabled' if not vision.enabled else 'remote' if vision.service.allow_remote else 'local'})
            elif path == '/api/jobs':
                self.respond_json({'jobs': self.store.listing()})
            elif path == '/api/assemblies':
                self.respond_json({'assemblies': self.store.assemblies.listing()})
            elif len(bits) == 3 and bits[:2] == ['api','assemblies']:
                self.respond_json(self.store.assemblies.snapshot(bits[2],full=True))
            elif len(bits) == 2 and bits[0] == 'assemblies':
                if bits[1] not in self.store.assemblies.records:raise KeyError(bits[1])
                if not path.endswith('/'):
                    self.send_response(302);self.send_header('Location',path+'/');self.end_headers()
                else:
                    self.send_file(Path(__file__).with_name('assembly_web')/'index.html')
            elif len(bits) == 3 and bits[0] == 'assemblies' and bits[2] in {'missing-face-source.png','missing-face-source.svg','missing-face-corrected.svg','missing-face-review.json'}:
                with self.store.lock:
                    project=self.store.assemblies.snapshot(bits[1],full=True)
                    report=project.get('missing_face_review')
                    requested=parse_qs(urlsplit(self.path).query).get('basis',[None])[0]
                    if not report or requested!=report.get('artifact_key',report['basis_signature']):
                        self.respond_json({'error':'回查依据已变化，请重新计算缺失面。'},409)
                    elif bits[2]=='missing-face-corrected.svg' and report.get('correction',{}).get('status')!='applied_candidate':
                        self.respond_json({'error':'本次回查未生成修正结构图，请查看回查原因。'},409)
                    elif bits[2] in {'missing-face-source.svg','missing-face-source.png'} and not report.get('source_overlay_available'):
                        self.respond_json({'error':'本次回查没有可用的原图裁剪。'},409)
                    else:
                        self.send_file(self.store.assemblies.root/bits[1]/bits[2])
            elif len(bits) == 2 and bits[0] == 'assembly-assets':
                if bits[1] in {'app.js','style.css','drawing-actions.js'}:
                    self.send_file(Path(__file__).with_name('assembly_web')/bits[1])
                elif bits[1] == 'member-labels.js':
                    self.send_file(Path(__file__).with_name('topology_web')/bits[1])
                elif bits[1] in {'three.module.js','three.core.js','OrbitControls.js'}:
                    self.send_file(Path(__file__).with_name('topology_web')/'vendor'/bits[1])
                else:self.send_error(404)
            elif len(bits) >= 3 and bits[:2] == ['api', 'jobs']:
                jid = bits[2]
                # Immutable files do not need the global job/assembly lock.
                # A large batch solve must not block another drawing's viewer.
                if jid not in self.store.jobs:raise KeyError(jid)
                if len(bits) == 3:
                    self.respond_json(self.store.snapshot(jid))
                elif len(bits) == 4 and bits[3] in {'source', 'report', 'log', 'input-report'}:
                    files = {'source': 'source.pdf', 'report': 'run/model-preflight.md', 'log': 'process.log', 'input-report': 'run/input-review.md'}
                    self.send_file(self.store.root / jid / files[bits[3]])
                else:
                    self.send_error(404)
            elif len(bits) >= 2 and bits[0] == 'results':
                jid = bits[1]
                if jid not in self.store.jobs:raise KeyError(jid)
                root = (self.store.root / jid / 'run/site').resolve()
                file = (root / '/'.join(bits[2:])).resolve()
                if not file.is_relative_to(root):
                    self.send_error(403)
                    return
                if file.is_dir():
                    if not path.endswith('/'):
                        self.send_response(302)
                        self.send_header('Location', path + '/')
                        self.end_headers()
                        return
                    file /= 'index.html'
                self.send_file(file)
            else:
                assets = Path(__file__).with_name('studio_web')
                if path not in {'/', '/app.js', '/style.css', '/favicon.ico'}:
                    self.send_error(404)
                    return
                if path == '/favicon.ico':
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_file(assets / ('index.html' if path == '/' else path[1:]))
        except (KeyError, FileNotFoundError):
            self.respond_json({'error': '未找到该图纸任务。'}, 404)
        except ValueError as exc:
            self.respond_json({'error':str(exc)},400)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not secrets.compare_digest(self.headers.get('X-Upload-Token', ''), self.token):
            self.respond_json({'error': '上传会话失效，请刷新页面后重试。'}, 403)
            return
        path = urlsplit(self.path).path
        bits = path.strip('/').split('/')
        try:
            if bits[:2] == ['api','assemblies'] and len(bits) in {2,3}:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=2*1024*1024:raise ValueError('装配请求大小无效。')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('装配请求必须是 JSON 对象。')
                if len(bits)==2:
                    self.respond_json(self.store.assemblies.create(body.get('job_ids'),body.get('title','横担层装配')),201)
                else:
                    self.respond_json(self.store.assemblies.mutate(bits[2],body))
                return
            if len(bits) == 4 and bits[:2] == ['api', 'jobs'] and bits[3] == 'retry':
                self.respond_json(self.store.retry(bits[2]), 202)
                return
            if path != '/api/jobs':
                self.respond_json({'error': '未知接口。'}, 404)
                return
            filename = unquote(self.headers.get('X-File-Name', 'drawing.pdf')).replace('\\', '/').split('/')[-1]
            if not filename.lower().endswith('.pdf') or len(filename) > 240:
                raise ValueError('请选择 PDF 文件，文件名不超过 240 个字符。')
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= MAX_UPLOAD:
                self.respond_json({'error': '单个 PDF 大小须在 1 字节至 100 MB 之间。'}, 413)
                return
            self.connection.settimeout(60)
            jid, folder = self.store.reserve(filename)
            target = folder / 'source.pdf'
            try:
                with target.open('xb') as stream:
                    remaining = length
                    first = True
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError('上传未完成，请重试。')
                        if first and b'%PDF-' not in block[:1024]:
                            raise ValueError('文件内容不是 PDF。')
                        first = False
                        stream.write(block)
                        remaining -= len(block)
            except Exception:
                target.unlink(missing_ok=True)
                folder.rmdir()
                raise
            self.respond_json(self.store.submit(jid, filename), 202)
        except AssemblyConflict as exc:
            self.respond_json({'error':str(exc)},409)
        except KeyError:
            self.respond_json({'error': '未找到需要重试的图纸。'}, 404)
        except (ValueError, TypeError, TimeoutError) as exc:
            self.respond_json({'error': str(exc)}, 400)


def create_studio(root, port=8770, config=None):
    store = JobStore(root, config)
    try:
        server = LocalHTTPServer(('0.0.0.0', port), partial(StudioHandler, store=store, token=secrets.token_urlsafe(32)))
    except Exception:
        store.close()
        raise
    return server, store


def serve_studio(root, port=8770, config=None):
    server, store = create_studio(root, port, config)
    print(f'Local drawing studio: http://127.0.0.1:{server.server_port}/', flush=True)
    print(f'Drawings and results: {store.root}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
