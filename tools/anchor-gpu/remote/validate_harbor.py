"""Run positive and negative transport fixtures through real CPU Harbor containers."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

from build_harbor_smoke import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:18765')
    parser.add_argument('--image', default='python:3.11-slim')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        image_id = subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        subprocess.run(['docker', 'pull', args.image], check=True)
        image_id = subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True).strip()
    image_info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image], text=True))[0]
    if not image_info.get('RepoDigests'):
        raise ValueError('CPU image must have an immutable repository digest')
    pinned_image = image_info['RepoDigests'][0]
    env = dict(os.environ, ZIWU_REMOTE_TOKEN=args.token_file.read_text().strip(), ZIWU_REMOTE_URL=args.url)
    summary = {'harbor_version': importlib.metadata.version('harbor'), 'cpu_image': pinned_image,
               'gpu_on_cpu_host': False, 'trials': []}
    for name, gravity, expected in [('positive', 9.81, 1.0), ('negative', 0.0, 0.0)]:
        config = build(args.output / name, pinned_image, gravity)
        with (args.output / (name + '.log')).open('w') as log:
            subprocess.run([str(Path(sys.executable).with_name('harbor')), 'run', '-c', str(config), '--yes'],
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        receipts = list((args.output / 'harbor-results' / name).glob('*/verifier/remote-result.json'))
        if len(receipts) != 1:
            raise RuntimeError('Expected one real remote receipt: ' + name)
        receipt = json.loads(receipts[0].read_text())
        reward = json.loads(receipts[0].with_name('reward.json').read_text())
        trial = json.loads((receipts[0].parent.parent / 'result.json').read_text())
        if trial.get('exception_info') or trial.get('verifier_result', {}).get('rewards') != reward:
            raise RuntimeError('Harbor did not complete verification cleanly: ' + name)
        if reward != {'score': expected, 'success': expected}:
            raise RuntimeError('Unexpected reward: ' + name)
        summary['trials'].append({'name': name, 'job_id': receipt['job_id'], 'reward': reward,
                                  'remote_hostname': receipt['result']['hostname'],
                                  'remote_pid': receipt['result']['pid'],
                                  'duration_sec': receipt['result']['duration_sec'],
                                  'exit_code': receipt['result']['exit_code'],
                                  'final_z': receipt['result']['trajectory'][-1]['z'],
                                  'harbor_exception': None})
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    if len({trial['remote_pid'] for trial in summary['trials']}) != 2:
        raise RuntimeError('Expected a fresh simulator process for each trial')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
