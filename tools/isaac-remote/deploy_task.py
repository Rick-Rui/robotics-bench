"""Install one task revision on the Robotics-Bench GPU pool host (used by /validate).

Sends `git archive <sha>:<task>` to the host job server's POST /deployments and
waits until the host reports the deployment installed. The host checks the
digest, fetches the runtime archive (cache or the task's pinned
install.runtime_archive_url) and runs the platform installer; no task code runs.
An already installed digest returns at once.
"""
import argparse
import gzip
import tarfile
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build import materialize  # noqa: E402


def call(url, token, method='GET', path='', data=None, headers=None):
    request = urllib.request.Request(url + path, data=data, method=method, headers={
        'Authorization': 'Bearer ' + token, **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')[:2000]
        raise RuntimeError(f'Host returned {exc.code} for {method} {path}: {body}') from None


def deploy(repo, sha, task_path, url, token, timeout=3600, poll=10, log=print):
    with tempfile.TemporaryDirectory(prefix='ziwu-deploy-') as temp:
        task_digest = materialize(Path(repo), sha, Path(temp) / 'task', task_path)
        # The bundle is the materialized tree (the task without its real-robot tier), so the digest
        # the host computes over it is the one above.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            for path in sorted((Path(temp) / 'task').rglob('*')):
                tar.add(path, arcname=str(path.relative_to(Path(temp) / 'task')), recursive=False)
    bundle = gzip.compress(buffer.getvalue(), mtime=0)
    health = call(url, token, path='/health')
    if not health.get('task_deployment'):
        raise RuntimeError('This host does not accept automatic task deployment')
    log(f'Task digest {task_digest}; bundle {len(bundle)} bytes')
    state = call(url, token, 'POST', '/deployments', bundle,
                 {'Content-Type': 'application/gzip', 'X-Task-Digest': task_digest})
    deadline = time.monotonic() + timeout
    while state['state'] == 'installing' and time.monotonic() < deadline:
        time.sleep(poll)
        state = call(url, token, path='/deployments/' + task_digest)
    if state['state'] == 'installing':
        state = dict(state, state='timed_out')
    state['task_digest'] = task_digest
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-repo', type=Path, required=True, help='git checkout that contains --sha')
    p.add_argument('--sha', required=True)
    p.add_argument('--task-path', required=True)
    p.add_argument('--url', default='http://127.0.0.1:18768')
    p.add_argument('--token-file', type=Path, required=True)
    p.add_argument('--timeout', type=int, default=3600)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try:
        state = deploy(a.task_repo, a.sha, a.task_path, a.url, a.token_file.read_text().strip(), a.timeout)
    except Exception as exc:
        state = {'state': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps({k: v for k, v in state.items() if k != 'log_tail'}))
    if state['state'] != 'installed':
        print(state.get('log_tail', ''))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
