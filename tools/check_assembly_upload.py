"""Run the existing real-PDF browser workflow against an isolated local studio,
then verify that one upload batch creates one persistent assembly project."""
import argparse
import json
from pathlib import Path
import sys
import threading
import uuid

from tower_rebuild.studio import create_studio
from check_studio_viewer import main as check_studio


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',action='append',type=Path,required=True)
    parser.add_argument('--out',type=Path,default=Path('output/assembly-upload-check'))
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    server,store=create_studio(args.out/('studio-'+uuid.uuid4().hex[:8]),0,'examples/workflow.native.json')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    url=f'http://127.0.0.1:{server.server_port}/'
    try:
        sys.argv=['check_studio_viewer.py','--url',url,'--out',str(args.out)]
        for path in args.pdf:sys.argv+=['--pdf',str(path)]
        check_studio()
        projects=store.assemblies.listing()
        assert len(projects)==1
        project=store.assemblies.snapshot(projects[0]['id'],True)
        assert len(project['sources'])==len(args.pdf)
        assert all(s['model_available'] for s in project['sources'])
        assert {f['source_id'] for f in project['faces']}=={s['id'] for s in project['sources']}
        assert not project['waiting']
        report={'pdf_count':len(args.pdf),'face_count':len(project['faces']),
                'status':project['status'],'shared_project':True,'all_models_loaded':True,
                'source_sha256':[s['source_sha256'] for s in project['sources']],
                'engineering_verified':False}
        (args.out/'assembly-upload-check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    finally:
        server.shutdown();server.server_close();store.close();worker.join()


if __name__=='__main__':main()
