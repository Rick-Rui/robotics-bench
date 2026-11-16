"""Install the frozen task runtime on a ziwu-gpu-v0 host (RTX 4090, Isaac Sim 5.1).

Verifies the runtime archive against the task manifest, copies the task's
environment/, tests/ and host/ unchanged, and registers the deployment. The
only rewrites are the runtime-archive rewrites the task declares in
host/service.json. Task files are never modified.

A runtime is either one archive (the manifest lists every file), or a shared
base package plus a small task archive: the manifest then names the base as
{"base": {"id": ..., "archive_sha256": ...}} and lists only the task's own
files. The base (its file map, rewrites and the paths a task may add) is read
from the platform's registry, bases/<id>.json next to this file, never from
the task. The base is unpacked first, then the task archive; a task file may
not replace a base file with different content.

Two entry points:
- --task DIR --archive FILE [--base-archive FILE]: an operator installs an
  unpacked task tree;
- --bundle FILE --expect-digest D: the host job server installs a task tree it
  received from /validate (a gzip tar of `git archive <sha>:<task>`). Each
  archive comes from the host's sha256-addressed cache, or is downloaded once:
  the task archive from the task's install.runtime_archive_url, the base from
  the archive_url of its registry record.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service as contract  # noqa: E402


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def replace(path, before, after, count=1):
    value = path.read_text()
    if value.count(before) != count:
        raise ValueError(f'Unexpected trusted source revision: {path.name}: {before[:60]}')
    path.write_text(value.replace(before, after))


def digest(task):
    """Task digest as computed on the Actions side (build.materialize)."""
    task = Path(task)
    files = {str(p.relative_to(task)): sha(p) for p in task.rglob('*') if p.is_file()}
    return files, hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def unpack(archive, runtime, files):
    """Extract an archive whose members are exactly the regular files named in `files`."""
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) != len(files) or {m.name for m in members} != set(files):
            raise ValueError('Archive member list differs')
        for member in members:
            p = Path(member.name)
            if not member.isfile() or p.is_absolute() or '..' in p.parts:
                raise ValueError('Unsafe archive member')
        source.extractall(runtime, filter='data')


def install(task, archive, root, engine_root, base_archive=None, bases=None):
    task, root = Path(task), Path(root).resolve()
    install_spec = contract.load_task(task)['install']
    manifest = json.loads((task / install_spec['manifest']).read_text())
    base = contract.runtime_base(manifest, bases)
    if sha(archive) != manifest['archive_sha256']:
        raise ValueError('Frozen runtime archive mismatch')
    files = manifest['files']
    if base:
        if base_archive is None or sha(base_archive) != base['archive_sha256']:
            raise ValueError('Runtime base archive mismatch: ' + base['id'])
        contract.check_task_files(base, files)
        files = {**base['files'], **files}
    if root.exists():
        raise ValueError('Fresh deployment directory required')
    runtime = root / 'runtime'
    runtime.mkdir(parents=True, mode=0o700)
    if base:
        unpack(base_archive, runtime, base['files'])
    unpack(archive, runtime, manifest['files'])
    for name, expected in files.items():
        if sha(runtime / name) != expected:
            raise ValueError('Frozen source/asset mismatch: ' + name)
    copied = [name for name in ('environment', 'tests', 'host') if (task / name).is_dir()]
    for name in copied:
        shutil.copytree(task / name, root / name)

    # Task files are never rewritten. A task may declare rewrites of files in
    # its runtime archive (for example an absolute path baked into an asset);
    # each one is applied exactly and recorded with before/after hashes. A
    # base package declares its own rewrites, applied first.
    changes = []
    for rewrite in (base.get('archive_rewrites', []) if base else []) + install_spec.get('archive_rewrites', []):
        path = runtime / rewrite['path']
        before = sha(path)
        replace(path, rewrite['before'], contract.expand(rewrite['after'], {'runtime': str(runtime)}),
                count=rewrite.get('count', 1))
        changes.append(dict(path=str(path.relative_to(root)), before=before, after=sha(path),
                            reason=rewrite.get('reason', '')))

    shader_hashes = {name: sha(Path(engine_root) / name) for name in manifest['engine_assets']}
    declared = install_spec['registration']
    registration = dict(schema=declared['schema'], case_id=declared['case_id'],
        runtime_root=str(runtime), assets_root=str(runtime / 'Assets'), isaac_version='5.1.0.0',
        native_action_limit=declared['native_action_limit'], verifier_timeout_sec=declared['verifier_timeout_sec'],
        source_files={n: sha(runtime / n) for n in files if not n.startswith('Assets/')},
        asset_files={str(runtime / n): sha(runtime / n) for n in files if n.startswith('Assets/')},
        **{field: manifest[field] for field in install_spec.get('manifest_fields', [])},
        engine_assets=shader_hashes)
    for item in install_spec.get('registrations', []):
        directory = root / item['verifier_root']
        reg = dict(registration, verifier_root=str(directory),
                   verifier_files={p.name: sha(p) for p in directory.glob('*.py')})
        (root / (item['name'] + '-registration.json')).write_text(json.dumps(reg, indent=2) + '\n')
    task_files, task_digest = digest(task)
    migration = dict(
        schema='ziwu.remote-bimanual-migration.v1', source_archive_sha256=manifest['archive_sha256'],
        source_task_files=task_files, task_digest=task_digest,
        installed_files={str(p.relative_to(root)): sha(p) for name in copied
                         for p in (root / name).rglob('*') if p.is_file()},
        changes=changes, isaac_version='5.1.0.0', engine_assets=shader_hashes)
    if base:
        migration['runtime_base'] = dict(id=base['id'], archive_sha256=base['archive_sha256'])
    (root / 'migration.json').write_text(json.dumps(migration, indent=2) + '\n')
    print(json.dumps({'installed': str(root), 'isaac_version': '5.1.0.0', 'archive_verified': True}))


BUNDLE_LIMIT = 32 * 1024**2          # compressed task tree sent by /validate
TREE_LIMIT = 256 * 1024**2           # unpacked task tree
TREE_FILES = 20000
ARCHIVE_LIMIT = 16 * 1024**3         # runtime archive download
_DIGEST = re.compile(r'[0-9a-f]{64}')


def unpack_bundle(bundle, destination):
    """Unpack a task tree received over the network into a fresh directory."""
    data = Path(bundle).read_bytes()
    if len(data) > BUNDLE_LIMIT:
        raise ValueError('Task bundle too large')
    destination = Path(destination)
    destination.mkdir()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        members = archive.getmembers()
        if len(members) > TREE_FILES:
            raise ValueError('Task bundle has too many files')
        for member in members:
            p = Path(member.name)
            if p.is_absolute() or '..' in p.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe task bundle member: ' + member.name)
            total += member.size
        if total > TREE_LIMIT:
            raise ValueError('Task bundle unpacks too large')
        archive.extractall(destination, filter='data')
    return destination


def fetch_archive(expected, declared_bytes, url, cache, endpoint=None, missing=''):
    """An archive from the sha256-addressed cache, downloading it once if needed."""
    if not _DIGEST.fullmatch(str(expected)):
        raise ValueError('Manifest archive_sha256 is not a sha256')
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (expected + '.tar.gz')
    if target.is_file():
        return target, 'cached'
    if not url:
        raise ValueError(missing)
    if endpoint:
        # Host-side mirror of the Hub (for example https://hf-mirror.com); same paths.
        url = endpoint.rstrip('/') + url[len('https://huggingface.co'):]
    limit = min(int(declared_bytes or ARCHIVE_LIMIT), ARCHIVE_LIMIT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    digest_ = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=cache, prefix='.part-', delete=False) as part:
        try:
            with opener.open(url, timeout=60) as response:
                while chunk := response.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Runtime archive larger than declared')
                    digest_.update(chunk)
                    part.write(chunk)
            if digest_.hexdigest() != expected:
                raise ValueError('Downloaded runtime archive does not match archive_sha256')
            part.flush()
            os.replace(part.name, target)
        except BaseException:
            Path(part.name).unlink(missing_ok=True)
            raise
    return target, 'downloaded'


def fetch_runtime(manifest, install_spec, cache, endpoint=None):
    """The task's runtime archive from the cache or its install.runtime_archive_url."""
    expected = manifest['archive_sha256']
    return fetch_archive(expected, manifest.get('archive_bytes'), install_spec.get('runtime_archive_url'),
                         cache, endpoint,
                         'Runtime archive ' + str(expected)[:12] + ' is not on this host and the task '
                         'declares no install.runtime_archive_url')


def fetch_base(base, cache, endpoint=None):
    """A base package from the cache or the archive_url of its registry record."""
    return fetch_archive(base['archive_sha256'], base.get('archive_bytes'), base.get('archive_url'), cache, endpoint,
                         'Runtime base ' + base['id'] + ' (' + base['archive_sha256'][:12] + ') is not on this '
                         'host and its registry record has no archive_url')


def install_bundle(bundle, expect_digest, deployments, cache, engine_root, endpoint=None, bases=None):
    """Install a task tree received from /validate into <deployments>/<digest>."""
    if not _DIGEST.fullmatch(expect_digest):
        raise ValueError('Invalid task digest')
    root = Path(deployments) / expect_digest
    if (root / 'migration.json').is_file():
        if json.loads((root / 'migration.json').read_text()).get('task_digest') == expect_digest:
            return dict(installed=str(root), state='already_installed')
    with tempfile.TemporaryDirectory(prefix='ziwu-task-') as temp:
        task = unpack_bundle(bundle, Path(temp) / 'task')
        actual = digest(task)[1]
        if actual != expect_digest:
            raise ValueError('Task bundle digest mismatch')
        if not (task / 'host/service.json').is_file():
            raise ValueError('Automatic deployment needs host/service.json')
        install_spec = contract.load_task(task)['install']
        manifest = json.loads((task / install_spec['manifest']).read_text())
        base = contract.runtime_base(manifest, bases)
        base_archive, base_source = fetch_base(base, cache, endpoint) if base else (None, None)
        archive, source = fetch_runtime(manifest, install_spec, cache, endpoint)
        if root.exists():
            shutil.rmtree(root)  # an earlier install of this digest did not finish
        try:
            install(task, archive, root, engine_root, base_archive, bases)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
    result = dict(installed=str(root), state='installed', runtime_archive=source)
    if base:
        result['runtime_base'] = base_source
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--engine-root', required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--task', help='unpacked task tree (operator install)')
    source.add_argument('--bundle', help='gzip tar of the task tree (host job server)')
    p.add_argument('--archive', help='runtime archive (with --task)')
    p.add_argument('--base-archive', help='base package archive, for a task whose manifest names a base (with --task)')
    p.add_argument('--expect-digest', help='task digest the bundle must have (with --bundle)')
    p.add_argument('--runtime-cache', help='sha256-addressed runtime archive cache (with --bundle)')
    p.add_argument('--hf-endpoint', default=os.environ.get('ZIWU_HF_ENDPOINT'),
                   help='Hugging Face mirror used for runtime downloads')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--root', help='explicit deployment directory')
    target.add_argument('--deployments', help='ZIWU_TASK_DEPLOYMENTS; installs into <deployments>/<task digest>')
    a = p.parse_args()
    if a.bundle:
        if not (a.expect_digest and a.runtime_cache and a.deployments):
            p.error('--bundle needs --expect-digest, --runtime-cache and --deployments')
        print(json.dumps(install_bundle(a.bundle, a.expect_digest, a.deployments, a.runtime_cache,
                                        a.engine_root, a.hf_endpoint)))
        raise SystemExit(0)
    if not a.archive:
        p.error('--task needs --archive')
    root = a.root or str(Path(a.deployments) / digest(a.task)[1])
    if Path(root).exists():
        raise SystemExit('Deployment already exists: ' + root)
    install(a.task, a.archive, root, a.engine_root, a.base_archive)
