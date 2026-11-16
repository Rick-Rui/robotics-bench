"""Build the runtime archives of a GPU task: a shared base package and a small task archive.

Tasks that use the same robot and scene do not each ship the whole runtime. The platform registers a base
package once (tools/isaac-remote/bases/<id>.json); a task archive then holds only the task's own files
(install.py, service.py).

Two uses:

  Split a complete runtime into a new base and the first task archive (maintainers, once per base):

    split_runtime.py --source runtime.tar.gz --out out/ --new-base robodojo-arx-v1 --base-meta meta.json \\
        --task-prefix Assets/Object/ --task-prefix private_layout/ \\
        --keep-in-base Assets/Object/RoboDojo/Geometry/camera_stand/ [--task-manifest old-manifest.json]

  Build the task archive of a task on an existing base (task authors):

    split_runtime.py --source my-runtime-tree/ --out out/ --base robodojo-arx-v1 [--task-manifest manifest.json]

--source is an unpacked runtime tree or a tar archive of one. Files under a task prefix go to the task archive
(except --keep-in-base paths, which belong to the scene and stay in the base); every other file belongs to the
base. With --base, a source file outside the task prefixes must be identical to the registered base file, and
the source may hold the task's files only.

Outputs in --out: task.tar.gz and task-manifest.json (with --task-manifest: that manifest with base, files,
archive_sha256 and archive_bytes replaced and its single-archive URL fields dropped); with --new-base also
base.tar.gz and <id>.json, the registry record. Archives are deterministic for one zlib build: members sorted
by name, mode 0644, mtime 0, no owner. Nothing is uploaded.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service as contract  # noqa: E402

SINGLE_ARCHIVE_FIELDS = ('review_archive_url', 'approved_archive_url')


def source_files(source):
    """Yield (name, bytes) for every regular file of a runtime tree or tar archive, sorted by name."""
    source = Path(source)
    if source.is_dir():
        for path in sorted(p for p in source.rglob('*') if p.is_file() or p.is_symlink()):
            if path.is_symlink():
                raise ValueError('Symlink in the runtime tree: ' + str(path.relative_to(source)))
            yield path.relative_to(source).as_posix(), path.read_bytes()
        return
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        for member in members:
            p = Path(member.name)
            if not member.isfile() or p.is_absolute() or '..' in p.parts:
                raise ValueError('Unsafe archive member: ' + member.name)
        if len({m.name for m in members}) != len(members):
            raise ValueError('Duplicate archive member')
        # Read in archive order (a gzip stream cannot seek cheaply), write sorted.
        if [m.name for m in members] == sorted(m.name for m in members):
            for member in members:
                yield member.name, archive.extractfile(member).read()
        else:
            data = {m.name: archive.extractfile(m).read() for m in members}
            for name in sorted(data):
                yield name, data[name]


class Writer:
    """A deterministic gzip tar; records the sha256 of every member."""

    def __init__(self, path):
        self.path = Path(path)
        self.files = {}
        self.raw = self.path.open('xb')
        self.gz = gzip.GzipFile(filename='', mode='wb', fileobj=self.raw, mtime=0, compresslevel=6)
        self.tar = tarfile.open(fileobj=self.gz, mode='w', format=tarfile.GNU_FORMAT)

    def add(self, name, data):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        self.tar.addfile(info, io.BytesIO(data))
        self.files[name] = hashlib.sha256(data).hexdigest()

    def close(self):
        self.tar.close()
        self.gz.close()
        self.raw.close()
        digest = hashlib.sha256()
        with self.path.open('rb') as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest(), self.path.stat().st_size


def split(source, out, task_prefixes, keep_in_base=(), new_base=None, base=None, base_meta=None,
          task_manifest=None):
    """Write the archives and manifests; returns (task manifest, base record or None)."""
    if (new_base is None) == (base is None):
        raise ValueError('Give exactly one of a new base id or an existing base record')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if base is not None:
        task_prefixes, keep_in_base = base['task_prefixes'], ()
    task_prefixes, keep_in_base = tuple(task_prefixes), tuple(keep_in_base)
    if not task_prefixes or any(not p.endswith('/') for p in task_prefixes + keep_in_base):
        raise ValueError('Task prefixes and keep-in-base paths must be given and end with /')

    def is_task(name):
        return name.startswith(task_prefixes) and not name.startswith(keep_in_base)

    task = Writer(out / 'task.tar.gz')
    base_writer = Writer(out / 'base.tar.gz') if new_base else None
    try:
        for name, data in source_files(source):
            if base is not None and name in base['files']:
                if hashlib.sha256(data).hexdigest() != base['files'][name]:
                    raise ValueError('Source file differs from base ' + base['id'] + ': ' + name)
            elif is_task(name):
                task.add(name, data)
            elif base is not None:
                raise ValueError('Source file is neither in base ' + base['id'] + ' nor under its task paths ('
                                 + ', '.join(task_prefixes) + '): ' + name)
            else:
                base_writer.add(name, data)
        if not task.files:
            raise ValueError('No task files found under ' + ', '.join(task_prefixes))
        task_sha, task_bytes = task.close()
        record = None
        if new_base:
            if not base_writer.files:
                raise ValueError('The base would be empty')
            base_sha, base_bytes = base_writer.close()
            meta = dict(base_meta or {})
            for key in ('schema', 'id', 'archive_sha256', 'archive_bytes', 'files', 'task_prefixes', 'kept_in_base'):
                meta.pop(key, None)
            archive_url = meta.pop('archive_url', None)
            record = dict(schema=contract.BASE_SCHEMA, id=new_base, **meta, archive_url=archive_url,
                          archive_sha256=base_sha, archive_bytes=base_bytes, task_prefixes=list(task_prefixes),
                          kept_in_base=list(keep_in_base), files=base_writer.files)
            contract.validate_base(record, new_base)
            (out / (new_base + '.json')).write_text(json.dumps(record, indent=1, sort_keys=False) + '\n')
            base = record
    except BaseException:
        for writer in (task, base_writer):
            if writer is not None:
                for handle in (writer.tar, writer.gz, writer.raw):
                    try:
                        handle.close()
                    except Exception:
                        pass
                writer.path.unlink(missing_ok=True)
        raise
    contract.check_task_files(base, task.files)
    manifest = dict(task_manifest or {})
    for field in SINGLE_ARCHIVE_FIELDS:
        manifest.pop(field, None)
    manifest.update(base=dict(id=base['id'], archive_sha256=base['archive_sha256']),
                    archive_sha256=task_sha, archive_bytes=task_bytes, files=task.files)
    layout = manifest.get('layout')
    if isinstance(layout, dict) and layout.get('path') in task.files and 'sha256' in layout:
        if layout['sha256'] != task.files[layout['path']]:
            raise ValueError('manifest layout.sha256 does not match the layout file in the task archive')
    (out / 'task-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest, record


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--source', required=True, help='unpacked runtime tree or tar archive')
    p.add_argument('--out', required=True, help='output directory (existing archives are not overwritten)')
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument('--base', help='id of a registered base (tools/isaac-remote/bases/<id>.json)')
    which.add_argument('--new-base', help='id of the base to create from the non-task files')
    p.add_argument('--bases', help='registry directory (default: bases/ next to this file)')
    p.add_argument('--task-prefix', action='append', default=[], help='with --new-base: a path a task archive may fill')
    p.add_argument('--keep-in-base', action='append', default=[],
                   help='with --new-base: a path under a task prefix that belongs to the scene')
    p.add_argument('--base-meta', help='with --new-base: JSON with description, provenance, licences, archive_rewrites')
    p.add_argument('--task-manifest', help='existing task manifest whose other fields are kept')
    a = p.parse_args()
    base = contract.load_base(a.base, a.bases) if a.base else None
    meta = json.loads(Path(a.base_meta).read_text()) if a.base_meta else None
    manifest = json.loads(Path(a.task_manifest).read_text()) if a.task_manifest else None
    task, record = split(a.source, a.out, a.task_prefix, a.keep_in_base, a.new_base, base, meta, manifest)
    summary = dict(task_archive_sha256=task['archive_sha256'], task_archive_bytes=task['archive_bytes'],
                   task_files=len(task['files']), base=task['base'])
    if record:
        summary.update(base_archive_bytes=record['archive_bytes'], base_files=len(record['files']))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
