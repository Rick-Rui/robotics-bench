"""Turn /validate's four GPU controls into the shared result row and acceptance record.

Writes <results>/<task>.json in the format render_validation_comment.py reads
(similarity, docker_build, oracle, nop, oracle_*_secs), a Markdown section
<results>/<task>.gpu.md with each control's measured result, and, when
accept.py passes, the acceptance record for tools/isaac-remote/evidence/.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from accept import accept  # noqa: E402

def controls_for(gpus):
    return [(harness, gpu) for gpu in gpus for harness in ('oracle', 'nop')]


def seconds(start, end):
    try:
        return round((datetime.fromisoformat(end.replace('Z', '+00:00'))
                      - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds(), 1)
    except (AttributeError, TypeError, ValueError):
        return None


def load(controls, harness, gpu):
    paths = list(Path(controls).glob(f'**/{harness}-gpu{gpu}-1/summary.json'))
    return (paths[0].parent, json.loads(paths[0].read_text())) if len(paths) == 1 else (None, None)


def report(task, task_tree, similarity, deploy, controls, gpus=(1, 2)):
    rows, dirs, ok = [], [], {'oracle': True, 'nop': True}
    oracle_secs = []
    for harness, gpu in controls_for(gpus):
        directory, data = load(controls, harness, gpu)
        expected = int(harness == 'oracle')
        if data is None:
            ok[harness] = False
            rows.append(f'| GPU {gpu} | {harness} | not run | | | |')
            continue
        dirs.append(directory)
        metrics = data.get('physical_metrics') or {}
        passed = (data.get('reward') == expected
                  and data.get('status') == ('passed' if expected else 'not_passed'))
        ok[harness] = ok[harness] and passed
        took = seconds(data.get('started_at'), data.get('finished_at'))
        if harness == 'oracle' and took is not None:
            oracle_secs.append(took)
        rows.append('| GPU {} | {} | {} | {} | {} | {} |'.format(
            gpu, harness, data.get('reward') if data.get('reward') is not None else data.get('status'),
            metrics.get('action_calls', ''), metrics.get('consistency_passed', ''),
            f'{int(took // 60)}m {int(took % 60):02d}s' if took is not None else ''))
    deployed = deploy.get('state') == 'installed'
    record, verdict = None, ''
    if deployed and len(dirs) == 4:
        try:
            record = accept(dirs, task_tree)
            verdict = f'`accept.py` passed: Oracle 1 and Nop 0 on GPU {gpus[0]} and GPU {gpus[1]}, same task digest.'
        except Exception as exc:
            verdict = f'`accept.py` failed: {exc}'
            ok['oracle'] = False
    result = dict(task=task, similarity='true' if similarity else 'false', docker_build='skipped',
                  oracle=('true' if ok['oracle'] else 'false') if deployed else '',
                  nop=('true' if ok['nop'] else 'false') if deployed else '',
                  oracle_total_secs=max(oracle_secs) if oracle_secs else None,
                  oracle_solve_secs=None, oracle_verify_secs=None)
    lines = [f'#### `{Path(task).name}` on the Robotics-Bench GPU pool (RTX 4090, Isaac Sim 5.1)', '']
    if deployed:
        lines += [f"Task digest `{deploy['task_digest'][:12]}`, installed on the host by this run.", '',
                  '| Lane | Control | Reward | Native actions | Replay consistent | Time |',
                  '| --- | --- | --- | --- | --- | --- |', *rows, '', verdict]
        if record:
            lines += ['', 'To record the acceptance in `main` (required before model trials), a maintainer runs:',
                      '', '```', f"tools/isaac-remote/land_evidence.sh {os.environ.get('GITHUB_RUN_ID', '<run id>')} {Path(task).name}", '```']
    else:
        lines += ['Not deployed on the GPU host, so no control ran:', '', '```',
                  (deploy.get('error') or deploy.get('state', 'unknown')).strip(),
                  (deploy.get('log_tail') or '').strip()[-3000:], '```']
    return result, '\n'.join(lines) + '\n', record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-path', required=True)
    p.add_argument('--task-tree', required=True)
    p.add_argument('--similarity', choices=('true', 'false'), required=True)
    p.add_argument('--deploy', type=Path, required=True)
    p.add_argument('--controls', type=Path, required=True)
    p.add_argument('--results', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--lanes', default='[1, 2]', help='JSON list of the two GPUs the controls ran on')
    a = p.parse_args()
    deploy = json.loads(a.deploy.read_text()) if a.deploy.is_file() else {'state': 'failed', 'error': 'No deployment result'}
    result, markdown, record = report(a.task_path, a.task_tree, a.similarity == 'true', deploy, a.controls,
                                      tuple(json.loads(a.lanes)))
    out = a.results / a.task_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix('.json').write_text(json.dumps(result) + '\n')
    out.with_suffix('.gpu.md').write_text(markdown)
    if record:
        a.evidence.parent.mkdir(parents=True, exist_ok=True)
        a.evidence.write_text(json.dumps(record, indent=2) + '\n')
    print(markdown)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
