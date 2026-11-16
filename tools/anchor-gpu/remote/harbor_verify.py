"""CPU verifier submits a frozen numeric candidate to the remote native worker."""
import json
import os
from pathlib import Path
import uuid

from client import Client
from protocol import canonical, digest, validate


def main():
    logs = Path('/logs/verifier')
    logs.mkdir(parents=True, exist_ok=True)
    candidate = Path('/app/output/candidate.json')
    if candidate.stat().st_size > 8192:
        raise ValueError('Candidate too large')
    parameters = json.loads(candidate.read_text())
    request = validate({'workload': 'isaac-drop-v1', 'gpu': 0, 'parameters': parameters})
    client = Client(os.environ['ZIWU_REMOTE_URL'], os.environ['ZIWU_REMOTE_TOKEN'])
    job = client.submit(request, 'harbor-' + uuid.uuid4().hex)
    (logs / 'remote-job.json').write_bytes(canonical({'job_id': job['job_id'], 'request': request}))
    try:
        job = client.wait(job['job_id'])
        if job['request_sha256'] != digest(request):
            raise ValueError('Result is not for this submission')
        (logs / 'remote-result.json').write_bytes(canonical(job))
        result = job['result']
        (logs / 'reward.json').write_bytes(canonical({'score': result['score'], 'success': float(result['success'])}))
    except BaseException as exc:
        try:
            client.cancel(job['job_id'])
        except Exception:
            pass
        (logs / 'error.json').write_bytes(canonical({'error': str(exc), 'job_id': job['job_id']}))
        raise  # No reward for transport, timeout or simulator errors.


if __name__ == '__main__':
    main()
