"""Render the completed trial's measured fields in the Actions job summary."""
import argparse
from datetime import datetime
import json
from pathlib import Path


def elapsed(start, end):
    if not start or not end:
        return 'N/A'
    seconds = max(0, int((datetime.fromisoformat(end.replace('Z', '+00:00'))
                         - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()))
    return f'{seconds // 60}m {seconds % 60:02d}s'


def report(root):
    summaries = sorted(root.glob('*/summary.json'))
    tasks = sorted({json.loads(p.read_text()).get('task_path', '') for p in summaries} - {''})
    lines = ['## GPU task result', '',
             'Task: ' + (', '.join('`' + t + '`' for t in tasks) or 'N/A') + '.', '',
             '| Measurement | Result |', '| --- | --- |']
    if not summaries:
        lines += ['| Task execution | No trial summary was produced |',
                  '| Reward | N/A; inspect the failed setup step |']
    for path in summaries:
        data = json.loads(path.read_text())
        control = data['harness'] in ('oracle', 'nop')
        metrics = data.get('physical_metrics') or {}
        agent = data.get('agent_execution') or {}
        reward = data.get('reward')
        receipts = list(path.parent.glob('trial/comparison/*/remote-evidence/verified.json'))
        remote = json.loads(receipts[0].read_text()) if len(receipts) == 1 else {}
        cleanup = list(path.parent.glob('trial/comparison/*/remote-evidence/cleanup.json'))
        clean = (json.loads(cleanup[0].read_text()).get('cleanup_confirmed')
                 if len(cleanup) == 1 else None)
        values = {
            'Harness / lane': f"{data['harness']} / GPU {remote.get('assigned_gpu', data.get('assigned_gpu', data['gpu']))}",
            'Run type': 'Oracle/Nop control; excluded from model scores' if control else 'Model trial',
            'Status': data['status'],
            'Reward': reward if reward is not None else 'N/A',
            'Harbor trial elapsed': elapsed(data.get('started_at'), data.get('finished_at')),
            'Agent execution elapsed': elapsed(agent.get('started_at'), agent.get('finished_at')),
            'Solver budget': '90 minutes',
            'Native score (diagnostic)': metrics.get('native_score_0_1', 'N/A'),
            'Native success': metrics.get('success', 'N/A'),
            'Independent replay consistent': metrics.get('consistency_passed', 'N/A'),
            'Recorded native action calls (includes final hold)': metrics.get('action_calls', 'N/A'),
            'Independent replay elapsed (seconds)': metrics.get('wall_seconds', 'N/A'),
            'Remote worker elapsed (seconds)': (remote.get('result') or {}).get('duration_sec', 'N/A'),
            'Cleanup confirmed': clean if clean is not None else 'N/A',
            'Input / cached / output tokens': ' / '.join(str(value) if value is not None else 'N/A'
                for value in ((data.get('usage') or {}).get(key)
                for key in ('n_input_tokens', 'n_cache_tokens', 'n_output_tokens'))),
            'Verified provider cost': 'N/A (CLI estimates are not verified billing)',
            'Engine': data['isaac_version'],
            'Source commit': data['source_sha'],
            'Task digest': data.get('task_digest', 'N/A'),
        }
        lines.extend(f'| {key} | {value} |' for key, value in values.items())
        if control and data['harness'] == 'nop':
            lines += ['', 'The no-action control is expected to return Reward=0.']
    lines += ['', 'Elapsed times cover only the named phase. Actions preparation and artifact upload '
              'are included in the workflow duration shown by GitHub.',
              'Full metrics, transport timings, execution logs and cleanup receipts are in the '
              '`bimanual-<harness>-gpu<lane>-<trial>` artifact.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(report(args.output))
