"""/scene on a tier:gpu PR: do the task's scene and assets hold up on the GPU pool? (docs/COMMANDS.md)

A scene check is the verify and deploy stages of /validate followed by ONE control session on ONE GPU, through
the full online path. It needs no working reference solution and no acceptance record, and it produces none:
no execution-checks result and no gpu-acceptance artifact come out of it, so /run, /cheat and /test stay gated
on /validate.

The session is the task's scene probe when the task ships one (solution/scene_probe.sh, see docs/SCENE_CHECK.md):
it observes, holds still for a few steps and saves the public camera frames under /app/output/scene/. Because
the hold is recorded, the host replays it, so the comment can also report replay consistency, the success
predicate after doing nothing, camera frames, and stillness when the task's verifier reports it
(metrics["scene_stillness"]). A task without a probe gets a no-action (Nop) session: it is not replayed
(metrics.status = "no_online_strategy") and returns no frames, so those rows are reported as not covered.

    COMMENT_BODY=... scene_check.py parse     {"command": "scene", "gpu": 1}, or {"command", "error"};
                                              exit 1 when the comment holds no /scene
    CONCLUSION=... HEAD_SHA=... STICKY=<json> scene_check.py static
                                              the static-checks verdict the verify stage uses: success,
                                              proposal_only, or the check-run conclusion
    scene_check.py comment --root <artifacts> --task-path <path> --run-url <url> [--sha <sha>] [--gpu N]
                           [--author <login>]  the PR comment (Markdown) on stdout; --author marks a run
                                              triggered by the PR author (counted by scene_trust.py quota)
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from trial_command import strip  # noqa: E402
from lanes import AUTO, lanes, parse_gpu  # noqa: E402

PROBE = 'solution/scene_probe.sh'
SCENE_DIR = ('app', 'output', 'scene')    # /app/output/scene/ in the agent container
# Stillness the task's verifier may report as metrics['scene_stillness']; the limits a still scene keeps.
STILL_LIMITS = dict(max_object_displacement_m=0.01, max_object_tilt_deg=5.0)
MARKER = '<!-- ziwu-scene-check author={} -->'
STICKY = '<!-- Sticky Pull Request Commentstatic-checks -->'
PROPOSAL_ANCHOR = '#check-task-proposal-link'
BOT = 'github-actions[bot]'


def static_verdict(conclusion, head_sha, sticky=None):
    """The static-checks result the verify stage of /scene accepts.

    conclusion: the latest completed `static-checks` check-run conclusion on head_sha. sticky: the static-checks
    sticky comment of the PR ({"user", "body"}), the place static-checks.yml lists its failed rows. A failing
    "Task proposal / issue link" check is accepted for in-house PRs and says nothing about a scene, so a run whose
    ONLY failed row is that check counts as passed for /scene ('proposal_only'). That needs a bot-written sticky
    comment for this very commit; anything else returns the conclusion unchanged. /validate is not affected."""
    if conclusion == 'success':
        return 'success'
    if conclusion != 'failure' or not isinstance(sticky, dict) or sticky.get('user') != BOT:
        return conclusion or 'missing'
    body = sticky.get('body') or ''
    if STICKY not in body or not head_sha or f'/commit/{head_sha}"' not in body:
        return conclusion
    m = re.search(r'<summary><b>(\d+) failed</b>.*?</summary>(.*?)</details>', body, re.S)
    if not m or m.group(1) != '1':
        return conclusion
    rows = [line for line in m.group(2).splitlines() if line.startswith('| [')]
    if len(rows) == 1 and PROPOSAL_ANCHOR + ')' in rows[0]:
        return 'proposal_only'
    return conclusion
IMAGES = ('.png', '.jpg', '.jpeg', '.webp')
ICONS = {True: '✅', False: '❌', None: '➖'}


def parse(body):
    m = re.search(r'(?:^|\s)/scene(?=\s|$)([^\n]*)', strip(body))
    if not m:
        return None
    options = dict(re.findall(r'(\w+)=([\w.,-]+)', m.group(1)))
    unknown = sorted(set(options) - {'gpu'})
    if unknown:
        return dict(command='scene', error='`/scene` does not accept ' + ', '.join(unknown)
                    + f'; available: `gpu=` one of {", ".join(map(str, lanes()))} (default: any free GPU).')
    try:
        gpu = parse_gpu(options.get('gpu', AUTO))
    except ValueError:
        return dict(command='scene', error=f'`gpu=` must be one of {", ".join(map(str, lanes()))}; omit it for any free GPU.')
    return dict(command='scene', gpu=gpu)


def seconds(start, end):
    try:
        return max(0.0, (datetime.fromisoformat(end.replace('Z', '+00:00'))
                         - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return None


def one(root, pattern):
    paths = sorted(Path(root).glob(pattern))
    if len(paths) != 1:
        return None, None
    try:
        return paths[0], json.loads(paths[0].read_text())
    except (OSError, ValueError):
        return paths[0], None


def evaluate(root):
    """Checks in stage order: [(name, ok, detail)], ok None = not run. Plus the measured facts."""
    _, verify = one(root, '**/verify.json')
    _, deploy = one(root, '**/deploy.json')
    summary_path, summary = one(root, '**/scene-probe-gpu*-1/summary.json')
    probe = summary_path is not None
    if not probe:
        summary_path, summary = one(root, '**/nop-gpu*-1/summary.json')
    _, receipt = one(root, '**/remote-evidence/verified.json')
    _, cleanup = one(root, '**/remote-evidence/cleanup.json')
    checks, facts = [], {}

    if verify is None:
        checks.append(('verify', False, 'no verification result; see the run log'))
    else:
        static = verify.get('static')
        failed = [name for name, key, good in (('static checks', 'static', ('success', 'proposal_only')),
                                               ('similarity check', 'similarity', ('true',)),
                                               ('preflight', 'preflight', ('true',))) if verify.get(key) not in good]
        detail = 'static checks, similarity check and preflight passed' if not failed else 'failed: ' + ', '.join(failed)
        if static == 'proposal_only':
            detail += '; the proposal check has not passed (it does not affect the scene check)'
        if 'preflight' in failed and verify.get('preflight_failed'):
            detail += ' (' + str(verify['preflight_failed'])[:300] + ')'
        if 'static checks' in failed:
            detail += f" (static checks on this commit: {verify.get('static') or 'missing'}; fix them or comment `/check`)"
        checks.append(('verify', not failed, detail))
    verified = checks[0][1]

    deployed = None
    if verified:
        if deploy is None:
            deployed, detail = False, 'no deployment result; see the run log'
        elif deploy.get('state') == 'installed':
            deployed, detail = True, f"task digest `{str(deploy.get('task_digest', ''))[:12]}` installed on the GPU host"
            facts['task_digest'] = deploy.get('task_digest')
        else:
            deployed = False
            detail = (str(deploy.get('error') or deploy.get('state') or 'unknown').strip().replace('\n', ' '))[:300]
    else:
        detail = 'not deployed: verification failed'
    checks.append(('deploy', deployed, detail))

    ready = sealed = zero = cleaned = None
    ready_detail = sealed_detail = zero_detail = clean_detail = 'not run'
    if deployed:
        if summary is None:
            ready, ready_detail = False, ('the probe' if probe else 'the nop run') + ' produced no result; see the run log'
        else:
            setup = (summary.get('agent_setup') or {}).get('started_at')
            error = summary.get('error') or summary.get('exception_info')
            ready = bool(setup) and summary.get('status') in ('passed', 'not_passed')
            if ready:
                took = seconds(summary.get('started_at'), setup)
                facts['ready_seconds'] = took
                ready_detail = 'the simulation service started and passed the readiness check' + (f', about {int(took)} s (including the agent container build)' if took is not None else '')
            else:
                text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
                ready_detail = ('not ready: ' + (text or summary.get('status') or 'unknown').replace('\n', ' '))[:400]
            if receipt is None:
                sealed, sealed_detail = (False, 'the host returned no receipt') if ready else (None, 'not run')
            else:
                facts['gpu_seconds'] = receipt.get('gpu_seconds')
                facts['queue_seconds'] = receipt.get('queue_seconds')
                try:
                    from verify_receipt import verify as verify_receipt
                    reward, metrics = verify_receipt(receipt)
                    facts['metrics'] = metrics
                    sealed = True
                    sealed_detail = 'recording sealed; the offline verification process completed'
                    if metrics.get('trusted_inputs_verified') is True:
                        sealed_detail += '; trusted inputs (runtime and asset hashes) verified'
                    zero = reward == 0 and summary.get('reward') == 0 and summary.get('status') == 'not_passed'
                    zero_detail = 'reward 0' if zero else f"reward {reward} ({'doing nothing' if probe else 'sending no action'} should score 0)"
                except Exception as exc:
                    sealed, sealed_detail = False, f'receipt rejected: {type(exc).__name__}: {exc}'[:300]
            if cleanup is None:
                cleaned, clean_detail = False, 'no cleanup receipt'
            else:
                cleaned = cleanup.get('cleanup_confirmed') is True
                clean_detail = 'GPU session ended' if cleaned else f"session state {cleanup.get('state')}"
    checks += [('ready', ready, ready_detail), ('sealed', sealed, sealed_detail),
               ('nop_zero', zero, zero_detail), ('cleanup', cleaned, clean_detail)]

    frames = scene_frames(summary_path, probe)
    assigned = next((g for g in ((receipt or {}).get('assigned_gpu'), (summary or {}).get('assigned_gpu'),
                                 (summary or {}).get('gpu')) if type(g) is int), None)
    facts.update(frames=sum(frames.values()), cameras=frames, gpu=assigned,
                 mode='probe' if probe else 'nop')
    if probe:
        checks += probe_checks(facts.get('metrics'), frames, sealed)
    return checks, facts


def scene_frames(summary_path, probe):
    """Image files the session left in its artifacts, per camera. A probe saves /app/output/scene/<camera>/<n>.png;
    a file directly under scene/ is counted under its name without the leading index. Without a probe, any image
    in the artifacts counts (under '-')."""
    counts = {}
    if summary_path is None:
        return counts
    for path in sorted(summary_path.parent.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in IMAGES or 'artifacts' not in path.parts:
            continue
        parts = path.parts
        at = next((i for i in range(len(parts) - len(SCENE_DIR)) if parts[i:i + len(SCENE_DIR)] == SCENE_DIR), None)
        if at is None:
            if not probe:
                counts['-'] = counts.get('-', 0) + 1
            continue
        inside = parts[at + len(SCENE_DIR):]
        camera = inside[0] if len(inside) > 1 else (re.sub(r'^[\d_.-]+', '', path.stem) or '-')
        counts[camera] = counts.get(camera, 0) + 1
    return counts


def stillness(metrics):
    """(ok, detail) from the optional metrics['scene_stillness'] of the task's verifier; ok None = not reported."""
    value = (metrics or {}).get('scene_stillness')
    if not isinstance(value, dict):
        return None, 'not reported: the task verifier did not report `scene_stillness`'
    if value.get('applicable') is False:
        return None, 'not applicable: the task declares that the scene moves from the start'
    numbers = {}
    for key in STILL_LIMITS:
        number = value.get(key)
        if type(number) not in (int, float) or number != number or number < 0:
            return None, f'not reported: `scene_stillness.{key}` is not a non-negative number'
        numbers[key] = float(number)
    ok = all(numbers[key] <= limit for key, limit in STILL_LIMITS.items())
    detail = (f"largest object displacement {numbers['max_object_displacement_m'] * 1000:.1f} mm, largest tilt "
              f"{numbers['max_object_tilt_deg']:.2f}° (limits {STILL_LIMITS['max_object_displacement_m'] * 1000:.0f} mm, "
              f"{STILL_LIMITS['max_object_tilt_deg']:.0f}°)")
    if isinstance(value.get('frames'), int):
        detail += f", {value['frames']} frames compared"
    return ok, detail


def probe_checks(metrics, frames, sealed):
    """The rows a scene probe adds: frames, replay, predicate, stillness."""
    total = sum(frames.values())
    if total:
        shot = ', '.join(f'{camera}: {count}' for camera, count in sorted(frames.items()))
        rows = [('frames', True, f'the probe saved {total} images ({shot}) in the run artifact `gpu-scene-frames-*`')]
    else:
        rows = [('frames', False, 'the probe saved no images under `/app/output/scene/`')]
    if not sealed or not isinstance(metrics, dict):
        return rows + [('replay', None, 'not run'), ('predicate', None, 'not run'), ('still', None, 'not run')]
    if metrics.get('status') != 'complete':
        return rows + [('replay', False, 'the probe left no recorded action, so the host did not replay; make the probe execute at least one hold-still step'),
                       ('predicate', None, 'not evaluated: no replay'), ('still', None, 'no replay')]
    consistent = metrics.get('consistency_passed') is True
    detail = 'the replay matches the online recording' if consistent else 'the replay does not match the online recording'
    for holder in ('consistency', 'replay'):
        evidence = metrics.get(holder)
        if isinstance(evidence, dict):
            compared = evidence.get('compared_frames', evidence.get('compared'))
            if isinstance(compared, int):
                detail += f', {compared} frames compared'
            if not consistent and evidence.get('first_failure') is not None:
                detail += '; first mismatch: ' + json.dumps(evidence['first_failure'], ensure_ascii=False)[:200]
            break
    success = metrics.get('success')
    rows += [('replay', consistent, detail),
             ('predicate', success is False,
              'the success predicate is still false after holding still' if success is False else 'the success predicate is already true although nothing was done')]
    return rows + [('still', *stillness(metrics))]


TITLES = {'verify': 'Verification (no GPU host)', 'deploy': 'Deployment', 'ready': 'Scene loaded, simulation ready',
          'sealed': 'Sealing and offline verification', 'nop_zero': 'No action scores 0', 'cleanup': 'Cleanup',
          'frames': 'Camera images', 'replay': 'Replay consistent', 'predicate': 'Predicate false when idle',
          'still': 'Scene at rest'}
OPTIONAL = ('still',)     # a row that may stay unreported without failing the scene check


def comment(root, task_path, run_url, sha='', gpu=None, author=''):
    checks, facts = evaluate(root)
    probe = facts.get('mode') == 'probe'
    passed = all(ok is True or (ok is None and name in OPTIONAL) for name, ok, _ in checks)
    lane = facts.get('gpu') if facts.get('gpu') is not None else (gpu if type(gpu) is int else None)
    lines = [f"### 🔍 GPU tier · scene check · `{Path(task_path).name}` {'✅' if passed else '❌'}", '']
    where = f'GPU {lane}' if lane is not None else 'any GPU'
    how = f"ran the task's scene probe (`{PROBE}`) once" if probe else 'ran one nop session'
    if sha:
        lines += [f'Commit `{sha[:7]}` on the Robotics-Bench GPU pool ({where}, RTX 4090, Isaac Sim 5.1): {how}. '
                  'No reference solution is needed and no acceptance record is produced.', '']
    titles = dict(TITLES, nop_zero='Doing nothing scores 0') if probe else TITLES
    lines += ['| Check | Result | Detail |', '|---|:-:|---|']
    lines += [f"| {titles[name]} | {ICONS[ok]} | {detail.replace('|', chr(92) + '|')} |" for name, ok, detail in checks]
    used = facts.get('gpu_seconds')
    if isinstance(used, (int, float)):
        waited = facts.get('queue_seconds')
        lines += ['', f'GPU time {used / 60:.1f} min' + (f', queued {waited:.0f} s.' if isinstance(waited, (int, float)) else '.')]
    metrics = facts.get('metrics') or {}
    if probe:
        lines += ['', 'The camera images are in the artifacts of this run. Download them and check by eye that every object is in '
                  'view and none intersects another object or floats.']
        if not any(name == 'still' and ok is not None for name, ok, _ in checks):
            lines += ['', '**Not covered by this check:** whether the scene stays at rest. That row gets a result once the task '
                      'verifier reports `scene_stillness` in `metrics.json` (see `docs/SCENE_CHECK.md`).']
    else:
        lines += ['', '**Not covered by this check:**', '']
        if metrics.get('status') == 'complete':
            lines += [f"- The task verifier also replayed the empty session: replay consistent "
                      f"`{metrics.get('consistency_passed')}`, native success `{metrics.get('success')}`."]
        else:
            lines += ['- Replay consistency, and whether the initial state already satisfies the success predicate: a session '
                      'without actions is not replayed, so neither was tested.']
        lines += ['- Physical stability (objects do not fly off, intersect or sink): this needs a run with actions and a '
                  'per-frame recording; use `/oracle` or `/validate`.',
                  f"- Camera images: {str(facts['frames']) + ' image(s) in the run artifacts.' if facts.get('frames') else 'a nop run takes no images, so the artifacts hold none.'}",
                  '', f'Add a scene probe to the task (`{PROBE}`; see `docs/SCENE_CHECK.md`) and `/scene` checks these as well.']
    lines += ['', 'A passing scene check is not an acceptance: `/run`, `/cheat` and `/test` still need a passing `/validate` '
              'whose acceptance record is merged into main.',
              '', f'<sub>[Run log and artifacts]({run_url})</sub>', '']
    if author:
        lines += [MARKER.format(author), '']
    return '\n'.join(lines)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('parse')
    sub.add_parser('static')
    c = sub.add_parser('comment')
    c.add_argument('--root', type=Path, required=True)
    c.add_argument('--task-path', required=True)
    c.add_argument('--run-url', required=True)
    c.add_argument('--sha', default='')
    c.add_argument('--gpu', type=parse_gpu, default=AUTO, help='the GPU requested: a lane or "auto"')
    c.add_argument('--author', default='', help='login of the PR author when the author triggered the run')
    a = p.parse_args()
    if a.action == 'static':
        sticky = os.environ.get('STICKY') or 'null'
        try:
            sticky = json.loads(sticky)
        except ValueError:
            sticky = None
        print(static_verdict(os.environ.get('CONCLUSION', ''), os.environ.get('HEAD_SHA', ''), sticky))
    elif a.action == 'parse':
        result = parse(os.environ.get('COMMENT_BODY', ''))
        if result is None:
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(comment(a.root, a.task_path, a.run_url, a.sha, a.gpu, a.author))
