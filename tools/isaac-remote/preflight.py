"""Verify stage of /validate for a GPU tier: everything that can be checked without the GPU host.

/validate on a tier:gpu PR runs verify -> deploy -> accept; a failed stage stops the next one, so a task
revision that fails here is never installed on the pool.

Checks (the task comes from --sha in --task-repo as data; nothing from it is executed):
  tier        the task offers a GPU tier (a ziwu-gpu task, or every GPU tier file of a multi-tier task)
  contract    host/service.json is a valid ziwu-gpu-service contract with an install section
  manifest    the install manifest exists and names a sha256 runtime archive; a base package it names is in
              the platform registry (bases/) at the pinned sha256, and the task files respect its task paths
  archive     install.runtime_archive_url is an https://huggingface.co/ URL that answers (HEAD), with the
              declared size when the Hub reports one; the same for the archive_url of a base package. An
              archive that is not hosted yet is reported as such
  build       the Harbor task for the agent and for the controls can be assembled from the tree
  evidence    reports whether main already holds an acceptance record for this digest (information only)

    preflight.py --task-repo pr --sha <sha> --task-path <path> [--tier gpu] --output preflight.json [--offline]

Exit 0 when every check passed, 1 otherwise; the JSON lists each check with ok / detail.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build import build, materialize, tiers  # noqa: E402
import service as contract  # noqa: E402

DIGEST = re.compile(r'[0-9a-f]{64}')
HUB = 'https://huggingface.co/'


def head(url, timeout=30):
    request = urllib.request.Request(url, method='HEAD')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.headers.get('X-Linked-Size') or response.headers.get('Content-Length')


def preflight(repo, sha, task_path, tier='', offline=False, head=head, bases=None):
    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append(dict(name=name, ok=True, detail=detail or ''))
            return True
        except Exception as exc:
            checks.append(dict(name=name, ok=False, detail=type(exc).__name__ + ': ' + str(exc)))
            return False

    result = dict(task_path=task_path, source_sha=sha, tier=tier or None, checks=checks)
    with tempfile.TemporaryDirectory(prefix='ziwu-preflight-') as temp:
        source = Path(temp) / 'task'
        result['task_digest'] = materialize(Path(repo), sha, source, task_path)

        def tier_check():
            offered = tiers(source)
            if 'gpu' not in offered:
                raise ValueError('the task has no GPU tier (host/service.json and the GPU tier files)')
            return ' + '.join(offered)
        state = {}

        def contract_check():
            state['service'] = contract.load_task(source)
            if 'install' not in state['service']:
                raise ValueError('host/service.json is missing: automatic deployment needs it')
            return state['service'].get('schema', '')

        def manifest_check():
            spec = state['service']['install']
            state['manifest'] = json.loads((source / spec['manifest']).read_text())
            if not DIGEST.fullmatch(str(state['manifest'].get('archive_sha256', ''))):
                raise ValueError('manifest archive_sha256 is not a sha256')
            detail = 'runtime archive ' + state['manifest']['archive_sha256'][:12]
            state['base'] = contract.runtime_base(state['manifest'], bases)
            if state['base']:
                contract.check_task_files(state['base'], state['manifest'].get('files'))
                detail += ' on base ' + state['base']['id'] + ' ' + state['base']['archive_sha256'][:12]
            return detail

        def archive_check():
            manifest, base = state['manifest'], state.get('base')
            sources = [('runtime archive', manifest['archive_sha256'], manifest.get('archive_bytes'),
                        state['service']['install'].get('runtime_archive_url'),
                        'host/service.json has no install.runtime_archive_url')]
            if base:
                sources.insert(0, ('runtime base ' + base['id'], base['archive_sha256'], base.get('archive_bytes'),
                                   base.get('archive_url'), 'its registry record has no archive_url'))
            missing = [f'{what} {digest[:12]} ({declared or "?"} bytes) is not hosted yet: {why}'
                       for what, digest, declared, url, why in sources if not url]
            if missing:
                raise ValueError('; '.join(missing))
            details = []
            for what, digest, declared, url, why in sources:
                if not url.startswith(HUB):
                    raise ValueError(('install.runtime_archive_url' if what == 'runtime archive' else what + ' archive_url')
                                     + ' must be an ' + HUB + ' URL')
                if offline:
                    continue
                status, size = head(url)
                if status != 200:
                    raise ValueError(f'{what} URL answered {status}')
                if declared and size and int(size) != int(declared):
                    raise ValueError(f'{what} is {size} bytes, {declared} declared')
                details.append(f'{what}: {size or "?"} bytes on the Hub')
            return 'not fetched (--offline)' if offline else '; '.join(details)

        def build_check():
            for control in (False, True):
                build(source, Path(temp) / ('control' if control else 'agent'), control, tier or None)
            return 'agent and control tasks assemble'

        def evidence_check():
            path = HERE / 'evidence' / (Path(task_path).name + '.json')
            if not path.is_file():
                return 'no acceptance record on main yet'
            record = json.loads(path.read_text())
            same = record.get('task_digest') == result['task_digest']
            return 'acceptance record on main ' + ('matches this revision' if same else 'is for another revision')

        ok = check('tier', tier_check)
        ok = ok and check('contract', contract_check)
        ok = ok and check('manifest', manifest_check)
        ok = ok and check('archive', archive_check)
        ok = check('build', build_check) and ok
        check('evidence', evidence_check)
    result['passed'] = bool(ok)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--task-repo', type=Path, required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--task-path', required=True)
    p.add_argument('--tier', choices=('', 'gpu'), default='')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = preflight(a.task_repo, a.sha, a.task_path, a.tier, a.offline)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    for item in result['checks']:
        print(('✅ ' if item['ok'] else '❌ ') + item['name'] + ': ' + item['detail'])
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
