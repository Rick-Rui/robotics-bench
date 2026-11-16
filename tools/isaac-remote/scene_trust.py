"""May the AUTHOR of a tier:gpu PR run /scene on it? (docs/SCENE_CHECK.md)

The GPU host has no sandbox: whatever a task puts on it runs as trusted code. Every GPU command therefore needs
write access, with one exception this file decides: the PR author may run /scene when the task is DATA-ONLY, that
is, when nothing the author wrote is executed or imported on the host or builds the containers of the run. The
decision is made from git tree and blob data of the GitHub API alone; nothing from the PR is checked out or run.

A task tree is data-only when every file in it is one of

  platform   byte-identical (same git blob) to a file of the same name on the default branch under
             PLATFORM_PREFIXES: the vendored base package, the host-side serve/collect/verify scripts, the agent
             and verifier container definitions, the runtime bundle manifest (which pins the runtime archive by
             sha256). The name must match too, so a platform file cannot be copied over, say, json.py;
  document   instruction.md, gpu/instruction-gpu.md, README.md, NOTICE.md, LICENSE, task.toml, and anything under
             authoring/ or real/ (real/ is never sent to the pool): never on a host import path, never a build input;
  solution   anything under solution/: it runs only inside the agent container of the run, as an agent's code does;
  scene      a scene description (SCENE_FILES): JSON that platform code parses, held to SCENE_RULES so that it
             cannot carry a URL, a path outside its own assets/ directory, or an unbounded size;
  mesh       an object mesh next to a scene description, <scene dir>/assets/**/*.obj|.stl, within MESH_RULES
             (plain names, size limits). Platform code reads these as numbers only (vertices and faces; no
             includes, material libraries or references), so a mesh cannot carry code or a path;
  service    host/service.json equal to a platform template except for the leaves in SERVICE_FREE.

Anything else (an edited platform file, an extra module next to a host script, a symlink, a submodule) makes the
task not data-only, and a maintainer has to run /scene after reading the change.

    scene_trust.py check --repo O/R --sha <head> --task-path <path> [--base main]   prints {"data_only", "problems", ...}
    scene_trust.py quota --repo O/R --pr N --author LOGIN [--limit 3]             prints {"used", "limit", "allowed"}
    scene_trust.py local <task dir> [--platform <repo root>]                       the same decision on a local checkout

`local` needs no GitHub access: it compares the task directory with the platform files of the checkout it runs from
(or --platform). An author runs it before opening a PR to see whether they may run /scene themselves. It is only
as current as that checkout; the decision on the PR is made against the default branch.

Exit status is 0 whenever the answer could be computed; read the JSON.
"""
import argparse
import base64
import hashlib
from datetime import datetime, timedelta, timezone
import json
import math
import re
import subprocess

PLATFORM_PREFIXES = ('tools/isaac-remote/',)
DOCUMENTS = ('instruction.md', 'gpu/instruction-gpu.md', 'README.md', 'NOTICE.md', 'LICENSE', 'task.toml')
DOCUMENT_DIRS = ('authoring/', 'real/')
SOLUTION_DIR = 'solution/'
SCENE_FILES = ('environment/service/scene.json', 'tests/scene.json')
SERVICE = 'host/service.json'
# Leaves of host/service.json a task may set; everything else (argv, pythonpath, cwd, timeouts, rewrites) is the template's.
SERVICE_FREE = (('install', 'registration', 'case_id'), ('install', 'registration', 'native_action_limit'))
SCENE_RULES = dict(max_bytes=65536, max_depth=6, max_items=256, max_number=1.0e4,
                   string=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'))
# String values that are not plain names but carry no path or URL either: command-line flags and signed numbers,
# as in a scene's "oracle": {"args": ["--object", "block", "--target", "-0.05", "0.10"]}. The reference solution
# runs only in the agent container (never on the host); the flags are plain data to everything else.
FLAG = re.compile(r'--?[A-Za-z][A-Za-z0-9_-]{0,62}')
NUMBER = re.compile(r'[-+]?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?')
# Object meshes of a scene: <directory of the scene file>/assets/<plain names>.obj|.stl
MESH_RULES = dict(directory='assets/', extensions=('.obj', '.stl'), max_file_bytes=2 * 1024**2,
                  max_total_bytes=10 * 1024**2, max_files=64, max_depth=4)
QUOTA = 3                      # author-triggered scene checks per PR per 24 hours
AUTHOR_GPU = 1                 # the one lane an author-triggered scene check uses
MARKER = re.compile(r'<!-- ziwu-scene-check author=([A-Za-z0-9-]+) -->')
BOT = 'github-actions[bot]'


def mesh_path(path, rules=MESH_RULES):
    """Is this path (relative to a scene file's directory) a well-formed mesh path: assets/<plain names>.obj|.stl?"""
    if not path.startswith(rules['directory']) or not path.endswith(rules['extensions']):
        return False
    parts = path.split('/')
    return len(parts) - 1 <= rules['max_depth'] and all(SCENE_RULES['string'].fullmatch(part) for part in parts)


def scene_problems(raw, rules=SCENE_RULES, meshes=frozenset()):
    """Why these bytes are not an acceptable scene description ([] when they are). meshes: the mesh files that
    exist next to this scene file, as paths relative to its directory (assets/...); a string with a slash must
    be one of them."""
    if len(raw) > rules['max_bytes']:
        return [f"larger than {rules['max_bytes']} bytes"]
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=lambda name: float(name))
    except (UnicodeDecodeError, ValueError) as exc:
        return [f'not JSON ({type(exc).__name__})']
    problems = []

    def walk(node, where, depth):
        if depth > rules['max_depth']:
            problems.append(f'{where}: nested deeper than {rules["max_depth"]}')
        elif isinstance(node, dict):
            if len(node) > rules['max_items']:
                problems.append(f'{where}: more than {rules["max_items"]} keys')
            for key, item in node.items():
                if not rules['string'].fullmatch(key):
                    problems.append(f'{where}: key {key[:40]!r} is not a plain name')
                walk(item, f'{where}.{key[:40]}', depth + 1)
        elif isinstance(node, list):
            if len(node) > rules['max_items']:
                problems.append(f'{where}: more than {rules["max_items"]} items')
            for index, item in enumerate(node[:rules['max_items']]):
                walk(item, f'{where}[{index}]', depth + 1)
        elif isinstance(node, str):
            if '/' in node:
                if node not in meshes:
                    problems.append(f'{where}: {node[:60]!r} is not a mesh file of this task under assets/')
            elif not (rules['string'].fullmatch(node) or FLAG.fullmatch(node) or NUMBER.fullmatch(node)):
                problems.append(f'{where}: string {node[:40]!r} is not a plain name, flag or number '
                                '(no path, URL or whitespace)')
        elif isinstance(node, bool) or node is None:
            pass
        elif isinstance(node, (int, float)):
            if not math.isfinite(node) or abs(node) > rules['max_number']:
                problems.append(f'{where}: number out of range')
        else:
            problems.append(f'{where}: unsupported value')

    walk(value, '$', 0)
    if not isinstance(value, dict):
        problems.append('$: not an object')
    return problems[:20]


def strip_free(value, free=SERVICE_FREE):
    """A copy of a service description with the task-settable leaves removed."""
    value = json.loads(json.dumps(value))
    for path in free:
        node = value
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return value


def service_problems(raw, templates):
    """host/service.json must be a platform template with only the SERVICE_FREE leaves changed."""
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return ['not JSON']
    if not isinstance(value, dict):
        return ['not an object']
    if not templates:
        return ['the platform publishes no host/service.json template on the default branch yet']
    if not any(strip_free(value) == strip_free(template) for template in templates):
        return ['differs from every platform template outside ' + ', '.join('.'.join(p) for p in SERVICE_FREE)]
    problems = []
    registration = (value.get('install') or {}).get('registration') or {}
    if not isinstance(registration.get('case_id', ''), str) or not SCENE_RULES['string'].fullmatch(registration.get('case_id', 'x')):
        problems.append('install.registration.case_id is not a plain name')
    limit = registration.get('native_action_limit', 1)
    if type(limit) is not int or not 1 <= limit <= 10000:
        problems.append('install.registration.native_action_limit must be an integer from 1 to 10000')
    return problems


def classify(task_entries, platform_blobs, read_blob, templates=()):
    """task_entries: [{"path" (inside the task), "mode", "type", "sha"}]; platform_blobs: {blob sha: {file names}}
    of the default branch under PLATFORM_PREFIXES; read_blob(sha) -> bytes; templates: parsed platform service templates.
    Returns {"data_only", "problems", "author_files", "platform_files"}."""
    problems, author, platform = [], [], 0
    if not task_entries:
        return dict(data_only=False, problems=['the task tree is empty or could not be read'], author_files=[],
                    platform_files=0)
    def is_platform(entry):
        return entry['path'].rsplit('/', 1)[-1] in platform_blobs.get(entry['sha'], ())

    # Mesh files next to each scene description: {scene directory: {relative path: size}}
    scene_dirs = {scene.rsplit('/', 1)[0] + '/' for scene in SCENE_FILES}
    meshes = {directory: {} for directory in scene_dirs}
    regular = [e for e in task_entries if e.get('type') == 'blob' and e.get('mode') in ('100644', '100755')]
    for entry in regular:
        for directory in scene_dirs:
            relative = entry['path'][len(directory):]
            if entry['path'].startswith(directory + MESH_RULES['directory']) and mesh_path(relative):
                meshes[directory][relative] = entry.get('size')
    for directory, found in sorted(meshes.items()):
        sizes = list(found.values())
        if len(found) > MESH_RULES['max_files']:
            problems.append(f"{directory}assets/: more than {MESH_RULES['max_files']} mesh files")
        if any(type(size) is not int for size in sizes):
            problems.append(f'{directory}assets/: a mesh file has no size in the tree data')
        elif sum(sizes) > MESH_RULES['max_total_bytes']:
            problems.append(f"{directory}assets/: meshes total {sum(sizes)} bytes, more than {MESH_RULES['max_total_bytes']}")

    for entry in sorted(task_entries, key=lambda e: e['path']):
        path = entry['path']
        if entry.get('type') == 'tree':
            continue
        if entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755'):
            problems.append(f'{path}: not a regular file (symlink or submodule)')
        elif path == SERVICE:
            if is_platform(entry):
                platform += 1
            else:
                author.append(path)
                problems += [f'{path}: {p}' for p in service_problems(read_blob(entry['sha']), templates)]
        elif path in SCENE_FILES:
            author.append(path)
            beside = frozenset(meshes[path.rsplit('/', 1)[0] + '/'])
            problems += [f'{path}: {p}' for p in scene_problems(read_blob(entry['sha']), meshes=beside)]
        elif any(path.startswith(d) and path[len(d):] in meshes[d] for d in scene_dirs):
            author.append(path)
            if type(entry.get('size')) is int and entry['size'] > MESH_RULES['max_file_bytes']:
                problems.append(f"{path}: {entry['size']} bytes, more than {MESH_RULES['max_file_bytes']} for one mesh")
        elif path in DOCUMENTS or path.startswith(DOCUMENT_DIRS) or path.startswith(SOLUTION_DIR):
            author.append(path)
        elif is_platform(entry):
            platform += 1
        else:
            problems.append(f'{path}: not a platform file (no file of this name and content under '
                            + ' '.join(PLATFORM_PREFIXES) + ' on the default branch)')
    return dict(data_only=not problems, problems=problems[:40], author_files=author, platform_files=platform)


def quota(comments, author, now, limit=QUOTA):
    """comments: [{"user", "created_at", "body"}] of the PR. Author-triggered scene checks leave a marker in the
    bot's result comment, which the author cannot delete; count those of the last 24 hours."""
    since = now - timedelta(hours=24)
    used = 0
    for c in comments:
        if c.get('user') != BOT:
            continue
        try:
            when = datetime.fromisoformat(str(c.get('created_at', '')).replace('Z', '+00:00'))
        except ValueError:
            continue
        if when >= since and any(m.lower() == author.lower() for m in MARKER.findall(c.get('body') or '')):
            used += 1
    return dict(used=used, limit=limit, allowed=used < limit)


def gh(*args):
    return subprocess.run(['gh', 'api', *args], check=True, capture_output=True, text=True).stdout


def blob_sha(data):
    """The git blob id of these bytes (what the GitHub tree API reports as sha)."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def local(task_dir, platform_root):
    """classify() on a local task directory against the platform files under platform_root."""
    from pathlib import Path
    task_dir, platform_root = Path(task_dir), Path(platform_root)
    blobs, platform, templates = {}, {}, []
    for prefix in PLATFORM_PREFIXES:
        for f in sorted((platform_root / prefix).rglob('*')):
            if f.is_file() and '__pycache__' not in f.parts:
                data = f.read_bytes()
                sha = blob_sha(data)
                blobs[sha] = data
                platform.setdefault(sha, set()).add(f.name)
                if f.as_posix().endswith('/' + SERVICE):
                    try:
                        templates.append(json.loads(data))
                    except ValueError:
                        pass
    entries = []
    for f in sorted(task_dir.rglob('*')):
        if f.is_symlink():
            entries.append({'path': f.relative_to(task_dir).as_posix(), 'mode': '120000', 'type': 'blob', 'sha': ''})
        elif f.is_file():
            data = f.read_bytes()
            sha = blob_sha(data)
            blobs[sha] = data
            mode = '100755' if f.stat().st_mode & 0o111 else '100644'
            entries.append({'path': f.relative_to(task_dir).as_posix(), 'mode': mode, 'type': 'blob', 'sha': sha,
                            'size': len(data)})
    return classify(entries, platform, blobs.get, templates)


def tree(repo, ref):
    data = json.loads(gh(f'repos/{repo}/git/trees/{ref}?recursive=1'))
    if data.get('truncated'):
        raise ValueError('Repository tree is truncated; cannot decide')
    return data['tree']


def check(repo, sha, task_path, base='main'):
    prefix = task_path.rstrip('/') + '/'
    head = [dict(e, path=e['path'][len(prefix):]) for e in tree(repo, sha) if e['path'].startswith(prefix)]
    base_tree = tree(repo, base)
    platform = {}
    for e in base_tree:
        if e['type'] == 'blob' and e['path'].startswith(PLATFORM_PREFIXES):
            platform.setdefault(e['sha'], set()).add(e['path'].rsplit('/', 1)[-1])

    def read_blob(blob):
        return base64.b64decode(json.loads(gh(f'repos/{repo}/git/blobs/{blob}'))['content'])

    templates = []
    for e in base_tree:
        if e['type'] == 'blob' and e['path'].startswith(PLATFORM_PREFIXES) and e['path'].endswith('/' + SERVICE):
            try:
                templates.append(json.loads(read_blob(e['sha'])))
            except ValueError:
                pass
    return classify(head, platform, read_blob, templates)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('check')
    c.add_argument('--repo', required=True)
    c.add_argument('--sha', required=True)
    c.add_argument('--task-path', required=True)
    c.add_argument('--base', default='main')
    q = sub.add_parser('quota')
    q.add_argument('--repo', required=True)
    q.add_argument('--pr', required=True)
    q.add_argument('--author', required=True)
    q.add_argument('--limit', type=int, default=QUOTA)
    loc = sub.add_parser('local')
    loc.add_argument('task')
    loc.add_argument('--platform', default=str(__import__('pathlib').Path(__file__).resolve().parents[2]))
    a = p.parse_args()
    if a.action == 'local':
        print(json.dumps(local(a.task, a.platform), ensure_ascii=False, indent=1))
    elif a.action == 'check':
        print(json.dumps(check(a.repo, a.sha, a.task_path, a.base), ensure_ascii=False))
    else:
        rows = gh('--paginate', f'repos/{a.repo}/issues/{a.pr}/comments',
                  '--jq', '.[] | {user: .user.login, created_at, body}')
        comments = [json.loads(line) for line in rows.splitlines() if line.strip()]
        print(json.dumps(quota(comments, a.author, datetime.now(timezone.utc), a.limit)))
