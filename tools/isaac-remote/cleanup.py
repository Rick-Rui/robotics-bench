"""Bounded cancellation of this Actions job's recorded sessions, including failures."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'anchor-gpu/remote'))
from client import Client
from protocol import TERMINAL

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--url',default='http://127.0.0.1:18768')
    a=p.parse_args()
    c=Client(a.url,a.token_file.read_text(),request_timeout=120)
    for receipt in a.output.glob('**/remote-evidence/session.json'):
        data=json.loads(receipt.read_text()); job=c.status(data['job_id'])
        if job['request']!=data['request']:raise ValueError('Cleanup request mismatch')
        if job['state'] not in TERMINAL:c.cancel(job['job_id'])
        deadline=time.monotonic()+45
        while job['state'] not in TERMINAL and time.monotonic()<deadline:
            time.sleep(1); job=c.status(job['job_id'])
        confirmed=job['state'] in TERMINAL
        receipt.with_name('cleanup.json').write_text(json.dumps(dict(job_id=job['job_id'],
            state=job['state'],cleanup_confirmed=confirmed)))
        if not confirmed:raise RuntimeError('Remote cancellation not confirmed')
