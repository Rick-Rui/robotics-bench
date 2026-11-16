"""Trusted online simulator -> sealed collection -> network-isolated replay.

The scheduler owns the Anchor GPU lock for this worker's entire lifetime.
No solver code, model credentials or solver-writable recording enters this host.
The three phases come from the task's host/service.json (see service.py).
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service as contract  # noqa: E402


def write(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, allow_nan=False) + '\n')
    temp.replace(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--deployment', type=Path, required=True)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--python', required=True)
    p.add_argument('--online-limit', type=int, default=6600)
    a = p.parse_args()
    root, out = a.deployment.resolve(), a.directory.resolve()
    migration = json.loads((root/'migration.json').read_text())
    for name, expected in migration['installed_files'].items():
        with (root/name).open('rb') as stream:
            actual = hashlib.sha256(stream.read()).hexdigest()
        if actual != expected: raise ValueError('Registered task adapter changed: '+name)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file = out / 'session-state.json'
    state = {'state': 'starting', 'model_calls': 0, 'gpu': int(os.environ['ZIWU_GPU_INDEX'])}
    write(state_file, state)
    service = contract.load(root)
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(str(root/item) for item in service.get('pythonpath', []))
    env.update(ACCEPT_EULA='Y', OMNI_KIT_ACCEPT_EULA='YES', PRIVACY_CONSENT='Y',
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', ZIWU_TASK_PYTHON=a.python)
    cwd = root / service.get('cwd', '.')
    online = out / 'online/session'
    values = dict(root=str(root), python=a.python, online=str(online), recording=str(out/'recording'),
                  verification=str(out/'verification'), online_limit=str(a.online_limit))
    command = contract.command(service, 'serve', values)
    ready = Path(contract.expand(service['serve']['ready_file'], values))
    startup_limit = service['serve'].get('startup_timeout_sec', 900)
    process = None
    try:
        with (out/'online.log').open('w') as log:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
            started = time.monotonic()
            while not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError(f'Online simulator startup exited {process.returncode}; see online.log')
                if time.monotonic() - started > startup_limit:
                    raise TimeoutError('Online simulator startup deadline')
                time.sleep(0.25)
            state.update(state='ready', started_at=time.time())
            write(state_file, state)
            while not (out/'finish-command.json').exists():
                if process.poll() not in (None, 0):
                    raise RuntimeError('Online native process failed')
                if time.monotonic() - started > a.online_limit + 900:
                    raise TimeoutError('Orchestrator did not finish this session')
                time.sleep(0.25)
            state.update(state='collecting'); write(state_file, state)
            # Collector accepts explicit private paths; the agent only has the
            # public numeric RPC capability and cannot create finish-command.
            subprocess.run(contract.command(service, 'collect', values), cwd=cwd, env=env, stdout=log,
                           stderr=subprocess.STDOUT, timeout=service['collect'].get('timeout_sec', 7250), check=True)
            if process.wait(timeout=30):
                raise RuntimeError('Native supervisor did not exit normally')
        state.update(state='replaying'); write(state_file, state)
        command = [sys.executable, str(Path(__file__).with_name('network_guard.py')),
                   *contract.command(service, 'verify', values)]
        # Fresh process, clean environment and inherited kernel network filter.
        with (out/'replay.log').open('w') as log:
            subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=service['verify'].get('timeout_sec', 7250), check=True)
        metrics = json.loads((out/'verification/metrics.json').read_text())
        reward = (out/'verification/reward.txt').read_text().strip()
        if reward not in ('0', '1'):
            raise RuntimeError('Verifier did not emit a binary reward')
        state.update(state='complete', reward=int(reward), metrics=metrics,
                     network_isolation='inherited-seccomp-deny-network-sockets', completed_at=time.time())
        write(state_file, state)
    except BaseException as exc:
        state.update(state='failed', error=f'{type(exc).__name__}: {exc}')
        write(state_file, state)
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


if __name__ == '__main__':
    main()
