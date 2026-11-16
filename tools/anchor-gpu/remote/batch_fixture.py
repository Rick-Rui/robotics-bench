"""Exercise CPU Docker -> bounded dispatcher -> GPU with numeric transport fixtures.

This is NOT an EmbodiedSWE model run. No candidate Python executes on the host.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import uuid

from client import Client
from pipeline import Pipeline
from protocol import canonical, validate


def docker_prepare(image, item):
    request = validate({'workload': 'isaac-drop-v1', 'gpu': item.get('gpu', 'auto'),
                        'parameters': item['parameters'],
                        'scheduling': {'trial': item.get('trial', item['id']),
                                       'phase': item.get('phase', 'final')}})
    name = 'ziwu-cpu-fixture-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '-i', '--network', 'none',
               '--cpus', '1', '--memory', '256m', '--pids-limit', '64', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65534',
               image, 'python', '-c', 'import json,sys; json.dump(json.load(sys.stdin),sys.stdout)']
    try:
        output = subprocess.run(command, input=canonical(request), capture_output=True,
                                check=True, timeout=60).stdout
    finally:
        # Also handles Docker CLI timeout: killing its client does not kill a container.
        removed = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
        if removed.returncode and b'No such container' not in removed.stderr:
            raise RuntimeError('CPU container cleanup failed: ' + name)
    return validate(json.loads(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--image', required=True, help='Locally available CPU Python image pinned by sha256 digest')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:18765')
    parser.add_argument('--cpu-slots', type=int, default=2)
    args = parser.parse_args()
    if '@sha256:' not in args.image:
        parser.error('Pin the CPU image by repository digest')
    client = Client(args.url, args.token_file.read_text())
    batch = Pipeline(client, args.journal, json.loads(args.manifest.read_text()),
                     lambda item: docker_prepare(args.image, item), cpu_slots=args.cpu_slots)
    try:
        result = asyncio.run(batch.run())
        print(json.dumps({'batch_id': result['batch_id'],
                          'states': {item['item']['id']: item['state'] for item in result['items']}}))
    finally:
        batch.close()


if __name__ == '__main__':
    main()
