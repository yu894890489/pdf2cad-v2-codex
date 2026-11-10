"""Bounded background CAD exports; immutable files remain tied to a snapshot."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from threading import Event, RLock
import uuid

from .io import read_json, write_json
from .part_drawings import model_basis, publish_part_drawings
from .final_bom import build_final_bom, load_original_tables


class PartDrawingJobs:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = jobs.root/'part-drawings'
        self.lock = RLock()
        self.stop = Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='part-cad')
        self.records = {}
        for path in self.root.glob('*/current.json'):
            value = read_json(path)
            if value.get('project_id') != path.parent.name:
                continue
            if value['status'] in {'queued', 'running'}:
                value.update(status='interrupted', error='上次服务中断，请重新生成。')
                write_json(path, value)
            self.records[path.parent.name] = value

    def save(self, aid, record):
        self.records[aid] = copy.deepcopy(record)
        write_json(self.root/aid/'current.json', record)

    def status(self, aid):
        snapshot = self.jobs.assemblies.snapshot(aid, full=True)
        with self.lock:
            value = copy.deepcopy(self.records.get(aid, {'project_id': aid, 'status': 'not_started'}))
        value.update(stale=value.get('basis_signature') not in {None, model_basis(snapshot)},
                     current_revision=snapshot['revision'])
        if value.get('export_id'):
            value['result_url'] = f'/assemblies/{aid}/part-drawings/{value["export_id"]}/index.html'
        return value

    def start(self, aid, revision):
        with self.jobs.lock:
            snapshot = self.jobs.assemblies.snapshot(aid, full=True)
            if revision != snapshot['revision']:
                raise ValueError('模型已更新，请刷新放样页面后重新生成。')
            if snapshot.get('waiting'):
                raise ValueError('图纸仍在处理中，请完成模型生成后再导出放样。')
            if not snapshot['faces']:
                raise ValueError('没有可用模型，请先完成图纸识别和装配。')
            sources, _, _ = self.jobs.assemblies.source_data(self.jobs.assemblies.records[aid])
            for source in sources:
                source.update(load_original_tables(self.jobs.root/source['id']/'run'))
            bom = build_final_bom(sources, snapshot)
        with self.lock:
            if self.stop.is_set():
                raise ValueError('服务正在关闭。')
            current = self.records.get(aid)
            if current and current['status'] in {'queued', 'running'}:
                return copy.deepcopy(current)
            if sum(v['status'] in {'queued','running'} for v in self.records.values()) >= 3:
                raise ValueError('放样任务队列已满，请等待当前任务完成。')
            record = {'project_id': aid, 'export_id': uuid.uuid4().hex, 'revision': snapshot['revision'],
                'basis_signature': model_basis(snapshot), 'status': 'queued', 'completed': 0, 'total': 0}
            self.save(aid, record)
            self.executor.submit(self._run, aid, snapshot, bom, record)
            return copy.deepcopy(record)

    def _run(self, aid, snapshot, bom, record):
        def progress(values):
            with self.lock:
                record.update(values, status='running')
                self.save(aid, record)
        try:
            progress({})
            folder = self.root/aid/record['export_id']
            result = publish_part_drawings(snapshot, bom, folder, progress=progress, cancel=self.stop.is_set)
            with self.lock:
                record.update(status=result['status'], counts=result['counts'], completed=result['completed'], total=result['total'])
                self.save(aid, record)
        except Exception as exc:
            with self.lock:
                record.update(status='failed', error=str(exc))
                self.save(aid, record)

    def artifact(self, aid, export_id, filename):
        if aid not in self.jobs.assemblies.records or not re.fullmatch(r'[a-f0-9]{32}', export_id):
            raise KeyError('export')
        folder = self.root/aid/export_id
        report = read_json(folder/'report.json')
        if report.get('project_id') != aid:
            raise KeyError('export')
        allowed = {'report.json', 'index.html', 'parts-dwg.zip'}
        allowed.update(part[key] for part in report['parts'] for key in ('dwg','dxf','svg') if part.get(key))
        if filename not in allowed or Path(filename).name != filename:
            raise KeyError('artifact')
        return folder/filename

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
