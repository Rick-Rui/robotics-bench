"""Deploy reviewed native workload code over the operator's existing SSH route."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True,
                        help='Private JSON with ssh (argv ending in bash -s), root, host_env and python')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--restart-idle', action='store_true', help='Replace this service only when its queue is idle')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    root = config['root']
    if not root.startswith('/') or any(c in root for c in '\n\r'):
        raise ValueError('root must be an absolute host path')
    args.token_file.parent.mkdir(parents=True, exist_ok=True)
    if not args.token_file.exists():
        fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(secrets.token_urlsafe(48) + '\n')
    if args.token_file.stat().st_mode & 0o077:
        raise ValueError('Token file must have mode 600')
    token = args.token_file.read_bytes()
    files = [Path(__file__).parents[1] / 'probe.py']
    files += [Path(__file__).with_name(name) for name in ['server.py', 'protocol.py', 'scheduling.py', 'isaac_drop.py']]
    task_tools = Path(__file__).parents[2] / 'isaac-remote'
    if config.get('bimanual_deployment'):
        files += [task_tools / name for name in ('worker.py', 'service.py', 'network_guard.py', 'install.py')]
        # Base package records: install.py reads them from bases/ next to itself.
        files += sorted((task_tools / 'bases').glob('*.json'))
    version = hashlib.sha256(b''.join(path.read_bytes() for path in files)).hexdigest()[:16]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path in files:
            name = 'bimanual_worker.py' if path.name == 'worker.py' else path.name
            folder = 'remote/bases/' if path.parent.name == 'bases' else (
                'remote/' if path.parent.name in ('remote', 'isaac-remote') else '')
            archive.add(path, arcname=folder + name)
    q = shlex.quote
    remote_code = root + '/releases/' + version
    service = '\n'.join([
        '#!/bin/bash', 'set -euo pipefail', 'umask 077', 'source ' + q(config['host_env']),
        *(['export PATH=' + q(config['video_bin']) + ':"$PATH"'] if config.get('video_bin') else []),
        *(['export ZIWU_BIMANUAL_DEPLOYMENT=' + q(config['bimanual_deployment'])]
          if config.get('bimanual_deployment') else []),
        *(['export ZIWU_TASK_DEPLOYMENTS=' + q(config['task_deployments'])]
          if config.get('task_deployments') else []),
        # Automatic installation from /validate (see server.Deployer).
        *(['export ZIWU_ENGINE_ROOT=' + q(config['engine_root'])] if config.get('engine_root') else []),
        *(['export ZIWU_RUNTIME_CACHE=' + q(config['runtime_cache'])] if config.get('runtime_cache') else []),
        *(['export ZIWU_HF_ENDPOINT=' + q(config['hf_endpoint'])] if config.get('hf_endpoint') else []),
        'exec 9>' + q(root + '/supervisor.lock'), 'flock -n 9 || exit 0',
        'echo $$ > ' + q(root + '/supervisor.pid'),
        '''trap 'kill "$child" 2>/dev/null || true; wait "$child" || true; exit 0' TERM INT''',
        'while true; do',
        '  python3 ' + q(remote_code + '/remote/server.py') + ' --root ' + q(root + '/jobs')
        + ' --token-file ' + q(root + '/token') + ' --lock-dir "$ZIWU_GPU_LOCK_DIR" --python '
        + q(config['python']) + ' --gpus ' + q(config.get('gpus', '0'))
        + ' --port ' + str(int(config.get('port', 18765))) + ' &',
        '  child=$!', '  wait "$child" || true', '  sleep 3', 'done', ''])
    script = 'set -euo pipefail\numask 077\nmkdir -p ' + q(root) + '\n'
    if args.restart_idle:
        restart = '\n'.join([
            'import os, pathlib, signal, sqlite3',
            'root = pathlib.Path(' + repr(root) + ')',
            "pidfile = root / 'supervisor.pid'",
            'if pidfile.exists():',
            '    pid = int(pidfile.read_text())',
            "    proc = pathlib.Path('/proc') / str(pid) / 'cmdline'",
            '    if proc.exists():',
            "        assert str(root / 'serve.sh').encode() in proc.read_bytes().split(b'\\0'), 'Supervisor PID identity mismatch'",
            "        db = sqlite3.connect(root / 'jobs/jobs.sqlite3')",
            '''        assert db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] == 0, 'Drain the queue first' ''',
            '        db.close()',
            '        os.kill(pid, signal.SIGTERM)',
        ])
        script += "python3 - <<'ZIWU_RESTART'\n" + restart + '\nZIWU_RESTART\n'
        script += 'flock -w 30 ' + q(root + '/supervisor.lock') + ' true\n'
        # The drained supervisor may remain as a zombie until its SSH parent reaps it.
        script += 'rm -f ' + q(root + '/supervisor.pid') + '\n'
    # Do not replace a live service or interrupt its jobs.
    script += 'if test -f ' + q(root + '/supervisor.pid') + '; then\n'
    script += '  if kill -0 "$(cat ' + q(root + '/supervisor.pid') + ')" 2>/dev/null; then echo "Existing supervisor: restart explicitly after draining jobs"; exit 1; fi\nfi\n'
    script += 'mkdir -p ' + q(remote_code) + '\n'
    script += 'base64 -d <<\'ZIWU_CODE\' | tar -xzf - -C ' + q(remote_code) + '\n'
    script += base64.b64encode(buffer.getvalue()).decode() + '\nZIWU_CODE\n'
    script += 'base64 -d > ' + q(root + '/token') + " <<'ZIWU_TOKEN'\n" + base64.b64encode(token).decode() + '\nZIWU_TOKEN\n'
    script += 'base64 -d > ' + q(root + '/serve.sh') + " <<'ZIWU_SERVICE'\n" + base64.b64encode(service.encode()).decode() + '\nZIWU_SERVICE\n'
    script += 'nohup bash ' + q(root + '/serve.sh') + ' >> ' + q(root + '/service.log') + ' 2>&1 < /dev/null &\n'
    subprocess.run(config['ssh'], input=script, text=True, check=True, timeout=90)
    print(json.dumps({'version': version, 'root': root, 'gpus': config.get('gpus', '0')}))


if __name__ == '__main__':
    main()
