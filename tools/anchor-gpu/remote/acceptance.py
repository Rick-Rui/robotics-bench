"""Bounded Actions CPU Docker -> GPU 1/2 transport acceptance, without a model."""
import argparse
import asyncio
import json
from pathlib import Path
import re

from batch_fixture import docker_prepare
from client import Client
from pipeline import Pipeline
from protocol import validate_result


def selected_gpus(value):
    if value not in ('1', '2', '1,2'):
        raise ValueError('Only the accepted PR8 GPU lanes 1, 2 or 1,2 are available')
    return tuple(int(gpu) for gpu in value.split(','))


def manifest(gpus=(1, 2)):
    if gpus not in ((1,), (2,), (1, 2)):
        raise ValueError('Unsupported PR8 GPU selection')
    return [dict(id=f'gpu{gpu}-{label}', gpu=gpu,
                 parameters=dict(steps=120, gravity=gravity, initial_height=1.0))
            for gpu in gpus for label, gravity in [('positive', 9.81), ('negative', 0)]]


def receipt(batch, gpus=(1, 2)):
    expected_manifest = manifest(gpus)
    if [row['item'] for row in batch['items']] != expected_manifest:
        raise ValueError('Acceptance manifest differs')
    rows = []
    for row in batch['items']:
        remote = row.get('remote') or {}
        if row['state'] != 'succeeded' or remote.get('assigned_gpu') != row['item']['gpu']:
            raise ValueError('Transport failed or wrong GPU affinity')
        result = validate_result(remote['result'], remote['request'])
        expected = float(row['item']['parameters']['gravity'] > 0)
        if result['score'] != expected or result.get('exit_code') != 0:
            raise ValueError('Control outcome or native exit differs')
        rows.append({key: remote[key] for key in
                     ('job_id', 'assigned_gpu', 'state', 'gpu_seconds', 'queue_seconds', 'request_sha256')}
                    | {'score': result['score'], 'success': result['success'], 'exit_code': result['exit_code']})
    if len({row['job_id'] for row in rows}) != len(expected_manifest):
        raise ValueError('Acceptance requires independent jobs for all selected controls')
    return dict(schema='ziwu.pr8-actions-transport.v1', status='passed',
                model_calls=0, benchmark_trials=0, selected_gpus=list(gpus), rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:18768')
    parser.add_argument('--gpus', choices=('1', '2', '1,2'), default='1,2')
    args = parser.parse_args()
    gpus = selected_gpus(args.gpus)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.image):
        raise ValueError('Expected an immutable Docker image ID')
    args.output.mkdir(parents=True, exist_ok=True)
    client = Client(args.url, args.token_file.read_text())
    batch = Pipeline(client, args.output / 'journal.json', manifest(gpus),
                     lambda item: docker_prepare(args.image, item), cpu_slots=2)
    try:
        result = receipt(asyncio.run(batch.run()), gpus)
        health = client.request('/health')
        result['queue_drained'] = health['queued'] == 0 and health['running'] == 0
        if not result['queue_drained']:
            raise ValueError('Dedicated acceptance queue did not drain')
        (args.output / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        # Explicitly cancel our own outstanding jobs on an interrupted/failed check.
        # Reusing the journal never starts a new paid attempt.
        for row in batch.data['items']:
            if row.get('job_id') and row['state'] == 'submitted':
                client.cancel(row['job_id'])
        batch.close()


if __name__ == '__main__':
    main()
