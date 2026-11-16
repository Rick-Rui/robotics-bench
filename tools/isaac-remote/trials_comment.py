"""PR comment for /oracle, /run, /cheat and /test on a tier:gpu PR: one row per trial.

    trials_comment.py --root <dir with */summary.json at any depth> --command run --task-path <path> \
        --run-url <Actions run URL> [--sha <head sha>]
"""
import argparse
from datetime import datetime
import json
from pathlib import Path

TITLES = {'oracle': 'Oracle', 'run': 'Agent trials', 'cheat': 'Cheat trials', 'test': 'Test trial'}
ICONS = {'passed': '✅', 'not_passed': '❌'}


def elapsed(start, end):
    if not start or not end:
        return '—'
    seconds = max(0, int((datetime.fromisoformat(end.replace('Z', '+00:00'))
                         - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()))
    return f'{seconds // 60}:{seconds % 60:02d}'


def comment(root, command, task_path, run_url, sha=''):
    rows = []
    for path in sorted(Path(root).rglob('summary.json')):
        data = json.loads(path.read_text())
        if 'harness' not in data:
            continue
        metrics = data.get('physical_metrics') or {}
        status = data.get('status', '')
        reward = data.get('reward')
        note = (data.get('error') or '').replace('|', '\\|').replace('\n', ' ')[:200]
        if 'acceptance is required' in note:
            note = 'this version has no GPU acceptance record on main: run `/validate`, then merge its acceptance record'
        gpu = data.get('assigned_gpu') if data.get('assigned_gpu') is not None else data.get('gpu')
        rows.append((data['harness'], 'any' if gpu == 'auto' else gpu, path.parent.name.rsplit('-', 1)[-1],
                     ICONS.get(status, '⚠️') + ' ' + status, '—' if reward is None else reward,
                     metrics.get('native_score_0_1', '—'), metrics.get('action_calls', '—'),
                     elapsed(data.get('started_at'), data.get('finished_at')), note))
    title = TITLES.get(command, command)
    repo_url = run_url.split('/actions/')[0]
    cheat = f' with the [cheat prompt]({repo_url}/blob/main/.github/hack-trial-prompt.md) prepended to the instruction' if command == 'cheat' else ''
    lines = [f'### 🖥️ GPU tier · {title} · `{Path(task_path).name}`', '',
             f'Commit `{sha[:7]}`, run on the Robotics-Bench GPU pool (RTX 4090, Isaac Sim){cheat}.' if sha else '', '']
    if rows:
        lines += ['| harness | GPU | trial | status | reward | native score | actions | time | note |',
                  '|---|:-:|:-:|---|:-:|:-:|:-:|:-:|---|']
        lines += ['| ' + ' | '.join(str(v) for v in row) + ' |' for row in rows]
        scored = [r[4] for r in rows if isinstance(r[4], (int, float))]
        if command in ('run', 'cheat', 'test') and scored:
            lines += ['', f'Passed {sum(1 for v in scored if v == 1)} / {len(scored)} (trials without a result are not counted).']
    else:
        lines += ['No trial produced a result; see the run log.']
    lines += ['', f'<sub>[Run log and artifacts]({run_url}): per-trial metrics, transfer timings, execution logs and cleanup receipts.</sub>', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--command', required=True)
    p.add_argument('--task-path', required=True)
    p.add_argument('--run-url', required=True)
    p.add_argument('--sha', default='')
    a = p.parse_args()
    print(comment(a.root, a.command, a.task_path, a.run_url, a.sha))
