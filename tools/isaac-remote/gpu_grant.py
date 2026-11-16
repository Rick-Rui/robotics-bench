"""GPU access for the author of a task's GPU tier (docs/LADDER.md, "GPU access").

Every task starts as a CPU tier. Once the CPU tier is reviewed and merged into main, the GPU pool is the
author's: on the PR that adds the GPU tier of the same task, the author may run /scene and /validate
themselves, within a daily quota. Everyone else who is not a maintainer still cannot.

The grant is decided from GitHub API data only; nothing from the PR is checked out or run:
  - the PR is the GPU tier of one task (ci_checks/check-task-tier.py);
  - <task>/instruction.md is on the default branch, so the CPU tier passed the merge gate (two reviewers'
    approvals on its head commit, .github/required-approvers.json) and the CPU checks;
  - the PR that first added <task>/instruction.md to the default branch was opened by this PR's author.
    A task that reached main another way (inherited, pushed directly) grants nothing;
  - the task on the default branch has a CPU tier: its task.toml does not declare a GPU-only validate_env
    ("ziwu-gpu", or the retired "modal"). A GPU-only task merged before the CPU-tier-first rule (the
    LEGACY_GPU_ONLY_PRS of ci_checks/check-task-tier.py) is valid, but no CPU tier of it was ever reviewed
    and merged, so it grants nothing; maintainers run its GPU commands.

    gpu_grant.py check --repo O/R --task-path P --author LOGIN [--base main]
        prints {"granted", "reason", "cpu_pr"}
    gpu_grant.py quota --repo O/R --pr N --author LOGIN --command validate|scene
        prints {"used", "limit", "allowed"}
    gpu_grant.py marker --author LOGIN --command validate|scene
        prints the marker the bot leaves in its comment when it accepts an author's command

Quotas, per PR per 24 hours: /scene 3 (counted by scene_trust.py's marker on the result comment), /validate 2
(counted by this module's marker on the bot's acknowledgement). The bot's comments are the count because the
author cannot delete them.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys
import tomllib
from urllib.parse import quote

GPU_ONLY_BACKENDS = ("ziwu-gpu", "modal")      # ci_checks/check-task-tier.py GPU_BACKENDS
BOT = 'github-actions[bot]'
LIMITS = {'scene': 3, 'validate': 2}
MARKERS = {
    'scene': re.compile(r'<!-- ziwu-scene-check author=([A-Za-z0-9-]+) -->'),    # written by gpu-scene-check.yml
    'validate': re.compile(r'<!-- ziwu-author-validate author=([A-Za-z0-9-]+) -->'),
}


def marker(command, author):
    return {'scene': '<!-- ziwu-scene-check author={} -->',
            'validate': '<!-- ziwu-author-validate author={} -->'}[command].format(author)


def decide(author, on_main, first_pr, gpu_only=False):
    """on_main: <task>/instruction.md is on the default branch. first_pr: {"number", "user", "merged"} of the
    PR that first added it there, or None. gpu_only: the task on the default branch declares a GPU-only
    validate_env, so it has no CPU tier."""
    if not on_main:
        return dict(granted=False, cpu_pr=None,
                    reason='The CPU tier of this task is not merged into main yet. Once it is reviewed and merged, the author '
                           'of the GPU tier can use the GPU pool directly.')
    if gpu_only:
        return dict(granted=False, cpu_pr=None,
                    reason='This task runs only on the GPU and has no merged CPU tier, so the GPU pool is not granted to the '
                           'author; maintainers run its GPU commands.')
    if not first_pr or not first_pr.get('merged'):
        return dict(granted=False, cpu_pr=None,
                    reason='No PR that merged the CPU tier of this task into main was found (the task was inherited or pushed '
                           'directly); maintainers run its GPU commands.')
    if str(first_pr.get('user', '')).lower() != author.lower():
        return dict(granted=False, cpu_pr=first_pr.get('number'),
                    reason=f"The CPU tier of this task is #{first_pr.get('number')} by another author; the GPU pool is "
                           "granted only to the author of the CPU tier.")
    return dict(granted=True, cpu_pr=first_pr.get('number'),
                reason=f"CPU tier #{first_pr.get('number')} is merged and has the same author.")


def quota(comments, author, command, now, limit=None):
    """comments: [{"user", "created_at", "body"}] of the PR."""
    limit = LIMITS[command] if limit is None else limit
    since = now - timedelta(hours=24)
    used = 0
    for c in comments:
        if c.get('user') != BOT:
            continue
        try:
            when = datetime.fromisoformat(str(c.get('created_at', '')).replace('Z', '+00:00'))
        except ValueError:
            continue
        if when >= since and any(m.lower() == author.lower() for m in MARKERS[command].findall(c.get('body') or '')):
            used += 1
    return dict(used=used, limit=limit, allowed=used < limit)


def gh(path, paginate=False):
    cmd = ['gh', 'api', path] + (['--paginate', '--slurp'] if paginate else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout or 'null')
    if paginate and isinstance(data, list):
        data = [x for page in data for x in (page if isinstance(page, list) else [page])]
    return data


def check(repo, task_path, author, base='main'):
    path = f'{task_path}/instruction.md'
    on_main = gh(f'repos/{repo}/contents/{quote(path)}?ref={quote(base)}') is not None
    first_pr = None
    gpu_only = False
    if on_main:
        toml = gh(f'repos/{repo}/contents/{quote(task_path + "/task.toml")}?ref={quote(base)}')
        try:
            env = tomllib.loads(base64.b64decode(toml['content']).decode()).get('environment', {})
        except (TypeError, KeyError, ValueError):
            return dict(granted=False, cpu_pr=None, reason="Could not read this task's task.toml on main.")
        gpu_only = env.get('validate_env') in GPU_ONLY_BACKENDS
        commits = gh(f'repos/{repo}/commits?sha={quote(base)}&path={quote(path)}&per_page=100', paginate=True) or []
        if commits:
            oldest = commits[-1]['sha']                      # the API lists newest first
            for pr in gh(f'repos/{repo}/commits/{oldest}/pulls') or []:
                if pr.get('merged_at') and pr.get('base', {}).get('ref') == base:
                    first_pr = dict(number=pr['number'], user=pr['user']['login'], merged=True)
                    break
    return decide(author, on_main, first_pr, gpu_only)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check')
    c.add_argument('--repo', required=True)
    c.add_argument('--task-path', required=True)
    c.add_argument('--author', required=True)
    c.add_argument('--base', default='main')
    q = sub.add_parser('quota')
    q.add_argument('--repo', required=True)
    q.add_argument('--pr', required=True)
    q.add_argument('--author', required=True)
    q.add_argument('--command', choices=sorted(LIMITS), required=True)
    m = sub.add_parser('marker')
    m.add_argument('--author', required=True)
    m.add_argument('--command', choices=sorted(LIMITS), required=True)
    a = p.parse_args()
    if a.cmd == 'marker':
        if not re.fullmatch(r'[A-Za-z0-9-]+', a.author):
            return 2
        print(marker(a.command, a.author))
        return 0
    if a.cmd == 'check':
        if not re.fullmatch(r'(tasks|lite)/[A-Za-z0-9_./-]+', a.task_path) or '..' in a.task_path:
            print(json.dumps(dict(granted=False, cpu_pr=None, reason='invalid task path')))
            return 0
        print(json.dumps(check(a.repo, a.task_path, a.author, a.base), ensure_ascii=False))
        return 0
    raw = gh(f'repos/{a.repo}/issues/{a.pr}/comments?per_page=100', paginate=True)
    if raw is None:
        return 1
    comments = [dict(user=c['user']['login'], created_at=c['created_at'], body=c.get('body') or '') for c in raw]
    print(json.dumps(quota(comments, a.author, a.command, datetime.now(timezone.utc))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
