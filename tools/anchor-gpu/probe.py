"""Inspect a selected Anchor GPU; explicitly requested CUDA/Isaac checks hold its lock."""
import argparse
import csv
import datetime
import fcntl
import json
import os
from pathlib import Path
import socket
import signal
import stat
import subprocess
import sys


def command(args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise RuntimeError('{} failed ({})'.format(args[0], result.returncode))
    return result.stdout.strip()


def acquire_lock(directory, gpu):
    path = Path(directory)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('GPU lock directory must be owned by this user, mode 700, and not a symlink')
    fd = os.open(str(path / 'anchor-robodojo-gpu-{}.lock'.format(gpu)),
                 os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        raise
    return fd


def run_runtime(args, env, log):
    process = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    def interrupted(signum, frame):
        raise InterruptedError('Runtime probe interrupted')
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return process.wait(timeout=300)
    finally:
        # Reap this probe's process group before releasing Anchor's lane lock.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        except ProcessLookupError:
            pass
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', required=True, choices=['0', '1', '2', '3'])
    parser.add_argument('--mode', choices=['inventory', 'cuda', 'isaac'], default='inventory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'ziwu.anchor-gpu.environment.v1', 'gpu_index': int(args.gpu),
              'mode': args.mode, 'status': 'failed', 'hostname': socket.gethostname(),
              'runner': os.environ.get('RUNNER_NAME'), 'source_sha': os.environ.get('GITHUB_SHA'),
              'run_id': os.environ.get('GITHUB_RUN_ID'),
              'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    lock = None
    try:
        lane = os.environ.get('ZIWU_RUNNER_GPU')
        if lane is not None and lane != args.gpu:
            raise ValueError('Requested GPU does not match the registered runner lane')
        fields = ['index', 'name', 'uuid', 'driver_version', 'memory.total']
        rows = list(csv.reader(command(['nvidia-smi', '--query-gpu=' + ','.join(fields),
                                        '--format=csv,noheader,nounits']).splitlines()))
        gpus = [dict(zip(fields, [v.strip() for v in row])) for row in rows]
        selected = next(gpu for gpu in gpus if gpu['index'] == args.gpu)
        if 'RTX 4090' not in selected['name']:
            raise ValueError('Selected GPU is not an RTX 4090')
        report['gpu'] = selected
        try:
            report['docker_version'] = command(['docker', 'version', '--format', '{{.Server.Version}}'])
            report['compose_version'] = command(['docker', 'compose', 'version', '--short'])
            report['docker_runtimes'] = sorted(json.loads(command(
                ['docker', 'info', '--format', '{{json .Runtimes}}'])))
            report['cdi_specs_present'] = any(list(Path(p).glob('*.yaml')) + list(Path(p).glob('*.json'))
                                              for p in ['/etc/cdi', '/var/run/cdi'])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            report['docker_error'] = str(exc)
        # Presence of runtime/CDI alone does not prove container GPU execution.
        report['embodiedswe_container_validation'] = 'not_run'
        if args.mode != 'inventory':
            lock = acquire_lock(os.environ['ZIWU_GPU_LOCK_DIR'], args.gpu)
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=selected['uuid'],
                       NVIDIA_VISIBLE_DEVICES=selected['uuid'], ZIWU_GPU_INDEX=args.gpu)
            with (args.output / 'runtime.log').open('w') as log:
                returncode = run_runtime([os.environ['ZIWU_SIM_PYTHON'],
                    str(Path(__file__).with_name('runtime_probe.py').resolve()),
                    str((args.output / 'runtime.json').resolve()), args.mode],
                    env=env, log=log)
            report['runtime_exit_code'] = returncode
            runtime_path = args.output / 'runtime.json'
            if not runtime_path.is_file():
                raise RuntimeError('Native runtime exited ({}) without a receipt; inspect local runtime.log'.format(returncode))
            runtime = json.loads(runtime_path.read_text())
            report['runtime'] = runtime
            if returncode or runtime.get('status') != 'passed':
                raise RuntimeError('Native runtime check failed; inspect local runtime.log')
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = '{}: {}'.format(type(exc).__name__, exc)
    finally:
        if lock is not None:
            os.close(lock)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
