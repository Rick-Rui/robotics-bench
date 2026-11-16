"""A runtime split into a platform base package and a task archive installs exactly like the single archive."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install  # noqa: E402
import preflight  # noqa: E402
import service as contract  # noqa: E402
import split_runtime  # noqa: E402
from test_install import CUROBO, LAUNCHER  # noqa: E402

BASE_FILES = {'Assets/Robots/x5/curobo.yml': CUROBO, 'Assets/Robots/x5/X5A.urdf': '<robot/>',
              'third_party/IsaacLab/source/isaaclab/isaaclab/app/app_launcher.py': LAUNCHER,
              'physical_rsi/sim.py': 'x = 1\n', 'Assets/Object/Scene/stand.usd': 'stand'}
TASK_FILES = {'Assets/Object/Rigid/toy/object.usd': 'toy', 'private_layout/toy.json': '{"layout": 0}\n'}
REWRITES = contract.LEGACY_SERVICE['install']['archive_rewrites']
OUTPUTS = ('online-registration.json', 'replay-registration.json')


def sha(text):
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode()).hexdigest()


def task_tree(directory, manifest, rewrites):
    """A task tree shaped like the bimanual task, around the given runtime manifest."""
    for d in ('environment/bundle', 'environment/runtime_common', 'tests/payload', 'host'):
        (directory / d).mkdir(parents=True)
    (directory / 'environment/bundle/manifest.json').write_text(json.dumps(manifest))
    (directory / 'environment/runtime_common/protocol.py').write_text('V = 1\n')
    (directory / 'tests/payload/verify.py').write_text('V = 2\n')
    (directory / 'instruction.md').write_text('task\n')
    service = json.loads(json.dumps(contract.LEGACY_SERVICE))
    service['install']['archive_rewrites'] = rewrites
    service['install']['registration']['case_id'] = 'toy-case'
    (directory / 'host/service.json').write_text(json.dumps(service))
    return directory


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        tree = root / 'tree'
        for name, text in {**BASE_FILES, **TASK_FILES}.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(text)
        (root / 'engine/kit/mdl').mkdir(parents=True)
        (root / 'engine/kit/mdl/Base.mdl').write_text('mdl')
        self.engine = root / 'engine'
        common = {'engine_assets': ['kit/mdl/Base.mdl'],
                  'layout': {'layout_id': 0, 'path': 'private_layout/toy.json', 'sha256': sha(TASK_FILES['private_layout/toy.json'])},
                  'action_limits': {'lower': [-1] * 14, 'upper': [1] * 14},
                  'review_archive_url': 'https://huggingface.co/datasets/someone/runtime/resolve/' + 'a' * 40 + '/r.tar.gz'}
        # The same runtime as one archive ...
        single = split_runtime.Writer(root / 'single.tar.gz')
        for name, data in split_runtime.source_files(tree):
            single.add(name, data)
        single_sha, single_bytes = single.close()
        self.single_task = task_tree(root / 'single-task', {**common, 'archive_sha256': single_sha,
                                                              'archive_bytes': single_bytes, 'files': single.files},
                                     REWRITES)
        # ... and split into a base package and a task archive.
        self.out = root / 'out'
        self.manifest, self.record = split_runtime.split(
            tree, self.out, ['Assets/Object/', 'private_layout/'], ['Assets/Object/Scene/'], new_base='toy-base',
            base_meta={'description': 'toy', 'archive_rewrites': REWRITES}, task_manifest=common)
        self.bases = root / 'bases'
        self.bases.mkdir()
        shutil.copy(self.out / 'toy-base.json', self.bases / 'toy-base.json')
        self.split_task = task_tree(root / 'split-task', self.manifest, [])

    def tearDown(self):
        self.temp.cleanup()

    def install_split(self, task=None, **kwargs):
        kwargs.setdefault('base_archive', self.out / 'base.tar.gz')
        install.install(task or self.split_task, self.out / 'task.tar.gz', self.root / 'deploy', self.engine,
                        bases=self.bases, **kwargs)

    def snapshot(self):
        deploy = self.root / 'deploy'
        files = {str(p.relative_to(deploy / 'runtime')): sha(p.read_bytes())
                 for p in (deploy / 'runtime').rglob('*') if p.is_file()}
        registrations = {name: json.loads((deploy / name).read_text()) for name in OUTPUTS}
        migration = json.loads((deploy / 'migration.json').read_text())
        shutil.rmtree(deploy)
        return files, registrations, migration


class SplitTests(Fixture):
    def test_the_split_puts_scene_files_in_the_base_and_task_files_in_the_task_archive(self):
        self.assertEqual(set(self.record['files']), set(BASE_FILES))
        self.assertEqual(set(self.manifest['files']), set(TASK_FILES))
        self.assertEqual(self.manifest['base'], {'id': 'toy-base', 'archive_sha256': self.record['archive_sha256']})
        self.assertNotIn('review_archive_url', self.manifest)
        self.assertEqual(self.record['task_prefixes'], ['Assets/Object/', 'private_layout/'])
        self.assertIsNone(self.record['archive_url'])
        with tarfile.open(self.out / 'base.tar.gz') as archive:
            members = archive.getmembers()
        self.assertEqual([m.name for m in members], sorted(BASE_FILES))
        self.assertEqual({(m.mode, m.mtime, m.uid, m.gid, m.uname) for m in members}, {(0o644, 0, 0, 0, '')})

    def test_archives_are_reproducible(self):
        again = self.root / 'again'
        manifest, record = split_runtime.split(
            self.root / 'tree', again, ['Assets/Object/', 'private_layout/'], ['Assets/Object/Scene/'],
            new_base='toy-base', base_meta={'archive_rewrites': REWRITES})
        self.assertEqual(record['archive_sha256'], self.record['archive_sha256'])
        self.assertEqual(manifest['archive_sha256'], self.manifest['archive_sha256'])

    def test_a_task_archive_is_built_against_a_registered_base(self):
        base = contract.load_base('toy-base', self.bases)
        only_task = self.root / 'only-task'
        for name, text in TASK_FILES.items():
            (only_task / name).parent.mkdir(parents=True, exist_ok=True)
            (only_task / name).write_text(text)
        manifest, record = split_runtime.split(only_task, self.root / 'o1', (), base=base)
        self.assertIsNone(record)
        self.assertEqual(manifest['archive_sha256'], self.manifest['archive_sha256'])
        # A full tree works too, as long as its other files are the base's.
        manifest, _ = split_runtime.split(self.root / 'tree', self.root / 'o2', (), base=base)
        self.assertEqual(manifest['files'], self.manifest['files'])
        (self.root / 'tree/physical_rsi/sim.py').write_text('x = 2\n')
        with self.assertRaisesRegex(ValueError, 'differs from base'):
            split_runtime.split(self.root / 'tree', self.root / 'o3', (), base=base)
        (only_task / 'env').mkdir()
        (only_task / 'env/extra.py').write_text('code\n')
        with self.assertRaisesRegex(ValueError, 'neither in base'):
            split_runtime.split(only_task, self.root / 'o4', (), base=base)


class InstallTests(Fixture):
    def test_base_plus_task_installs_the_same_runtime_and_registrations_as_the_single_archive(self):
        install.install(self.single_task, self.root / 'single.tar.gz', self.root / 'deploy', self.engine)
        single_files, single_reg, single_migration = self.snapshot()
        self.install_split()
        split_files, split_reg, split_migration = self.snapshot()
        self.assertEqual(set(split_files), set(BASE_FILES) | set(TASK_FILES))
        self.assertEqual(split_files, single_files)
        self.assertEqual(split_reg, single_reg)
        self.assertEqual(split_migration['changes'], single_migration['changes'])
        self.assertEqual(split_migration['runtime_base'],
                         {'id': 'toy-base', 'archive_sha256': self.record['archive_sha256']})
        self.assertNotIn('runtime_base', single_migration)
        self.assertEqual(split_migration['source_archive_sha256'], self.manifest['archive_sha256'])

    def test_single_archive_install_is_unchanged_from_main(self):
        old = subprocess.run(['git', 'show', 'origin/main:tools/isaac-remote/install.py'], cwd=HERE,
                             capture_output=True, text=True)
        if old.returncode or 'runtime_base' in old.stdout:
            self.skipTest('the previous install.py is not available in this checkout')
        (self.root / 'old').mkdir()
        (self.root / 'old/install.py').write_text(old.stdout)
        shutil.copy(HERE / 'service.py', self.root / 'old/service.py')
        outputs = {}
        for name, script in (('old', self.root / 'old/install.py'), ('new', HERE / 'install.py')):
            subprocess.run([sys.executable, str(script), '--task', str(self.single_task), '--archive',
                            str(self.root / 'single.tar.gz'), '--root', str(self.root / 'deploy'),
                            '--engine-root', str(self.engine)], check=True, capture_output=True)
            deploy = self.root / 'deploy'
            outputs[name] = {str(p.relative_to(deploy)): p.read_bytes() for p in deploy.rglob('*') if p.is_file()}
            shutil.rmtree(deploy)
        self.assertEqual(outputs['old'], outputs['new'])

    def test_a_task_archive_cannot_replace_base_files_or_leave_its_paths(self):
        for name, error in (('physical_rsi/sim.py', 'replaces a base file'), ('env/extra.py', 'outside the base')):
            manifest = json.loads(json.dumps(self.manifest))
            manifest['files'][name] = sha('other')
            task = task_tree(self.root / ('bad-' + name.replace('/', '-')), manifest, [])
            with self.assertRaisesRegex(ValueError, error):
                self.install_split(task)
            self.assertFalse((self.root / 'deploy').exists())
        # The same file with the same content is redundant, not a replacement.
        contract.check_task_files(self.record, {**self.manifest['files'],
                                                'physical_rsi/sim.py': sha(BASE_FILES['physical_rsi/sim.py'])})

    def test_a_missing_wrong_or_unknown_base_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'base archive mismatch'):
            self.install_split(base_archive=None)
        with self.assertRaisesRegex(ValueError, 'base archive mismatch'):
            self.install_split(base_archive=self.out / 'task.tar.gz')
        for change, error in (({'id': 'other-base'}, 'Unknown runtime base'),
                              ({'archive_sha256': 'f' * 64}, 'the registry holds'),
                              ({'id': '../toy-base'}, 'Invalid runtime base id')):
            manifest = json.loads(json.dumps(self.manifest))
            manifest['base'].update(change)
            task = task_tree(self.root / ('bad-base-' + sha(json.dumps(change))[:8]), manifest, [])
            with self.assertRaisesRegex(ValueError, error):
                self.install_split(task)
        self.assertFalse((self.root / 'deploy').exists())

    def test_a_changed_base_archive_member_is_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name, text in sorted(BASE_FILES.items()):
                data = (text + ('#' if name == 'physical_rsi/sim.py' else '')).encode()
                info = tarfile.TarInfo(name); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        forged = self.root / 'forged.tar.gz'
        forged.write_bytes(buffer.getvalue())
        record = json.loads((self.bases / 'toy-base.json').read_text())
        record['archive_sha256'] = sha(buffer.getvalue())
        (self.bases / 'toy-base.json').write_text(json.dumps(record))
        manifest = json.loads(json.dumps(self.manifest))
        manifest['base']['archive_sha256'] = record['archive_sha256']
        task = task_tree(self.root / 'forged-task', manifest, [])
        with self.assertRaisesRegex(ValueError, 'Frozen source/asset mismatch: physical_rsi/sim.py'):
            self.install_split(task, base_archive=forged)

    def test_the_host_installs_a_bundle_with_both_archives_from_its_cache(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            for path in sorted(p for p in self.split_task.rglob('*') if p.is_file()):
                tar.add(path, arcname=str(path.relative_to(self.split_task)), recursive=False)
        bundle = self.root / 'bundle.tar.gz'
        bundle.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
        digest = install.digest(self.split_task)[1]
        cache = self.root / 'cache'
        cache.mkdir()
        shutil.copy(self.out / 'task.tar.gz', cache / (self.manifest['archive_sha256'] + '.tar.gz'))
        with self.assertRaisesRegex(ValueError, 'Runtime base toy-base .* has no archive_url'):
            install.install_bundle(bundle, digest, self.root / 'deployments', cache, self.engine, bases=self.bases)
        shutil.copy(self.out / 'base.tar.gz', cache / (self.record['archive_sha256'] + '.tar.gz'))
        state = install.install_bundle(bundle, digest, self.root / 'deployments', cache, self.engine, bases=self.bases)
        self.assertEqual((state['state'], state['runtime_archive'], state['runtime_base']),
                         ('installed', 'cached', 'cached'))
        migration = json.loads((self.root / 'deployments' / digest / 'migration.json').read_text())
        self.assertEqual(migration['task_digest'], digest)


class RegistryTests(Fixture):
    def test_base_records_are_validated(self):
        good = json.loads((self.bases / 'toy-base.json').read_text())
        contract.validate_base(good, 'toy-base')
        for change in ({'schema': 'other'}, {'id': 'renamed'}, {'archive_sha256': 'short'}, {'archive_bytes': 0},
                       {'archive_url': 'https://example.com/base.tar.gz'}, {'files': {}},
                       {'files': {'../escape': 'a' * 64}}, {'files': {'ok/file': 'nothash'}},
                       {'task_prefixes': []}, {'task_prefixes': ['Assets/Object']}, {'task_prefixes': ['/abs/']},
                       {'archive_rewrites': [{'path': 'not/in/base', 'before': 'a', 'after': 'b'}]}):
            with self.assertRaises(ValueError, msg=change):
                contract.validate_base({**good, **change}, 'toy-base')
        pinned = 'https://huggingface.co/datasets/org/bases/resolve/' + 'a' * 40 + '/toy-base/base.tar.gz'
        contract.validate_base({**good, 'archive_url': pinned}, 'toy-base')

    def test_every_registered_base_is_valid(self):
        for path in sorted(contract.BASES.glob('*.json')):
            record = contract.load_base(path.stem)
            self.assertEqual(record['id'], path.stem)

    def test_a_manifest_without_a_base_is_a_single_archive_runtime(self):
        self.assertIsNone(contract.runtime_base({'archive_sha256': 'a' * 64, 'files': {}}))
        with self.assertRaisesRegex(ValueError, 'manifest base must be'):
            contract.runtime_base({'base': 'toy-base'}, self.bases)


class PreflightTests(Fixture):
    def repository(self, task, url=None):
        repo = self.root / 'repo'
        target = repo / 'tasks/x/y/z'
        target.parent.mkdir(parents=True)
        shutil.copytree(task, target)
        (target / 'task.toml').write_text('[environment]\nvalidate_env = "ziwu-gpu"\n')
        if url:
            service = json.loads((target / 'host/service.json').read_text())
            service['install']['runtime_archive_url'] = url
            (target / 'host/service.json').write_text(json.dumps(service))
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@t')
        for args in (('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'task')):
            subprocess.check_call(('git',) + args, cwd=repo, env=env)
        return repo, subprocess.check_output(('git', 'rev-parse', 'HEAD'), cwd=repo, text=True).strip()

    def checks(self, repo, sha_):
        result = preflight.preflight(repo, sha_, 'tasks/x/y/z', offline=True, bases=self.bases)
        return {c['name']: c for c in result['checks']}

    def test_archives_that_are_not_hosted_yet_are_named(self):
        checks = self.checks(*self.repository(self.split_task))
        self.assertTrue(checks['manifest']['ok'], checks['manifest'])
        self.assertIn('on base toy-base', checks['manifest']['detail'])
        self.assertFalse(checks['archive']['ok'])
        self.assertIn('runtime base toy-base', checks['archive']['detail'])
        self.assertIn('its registry record has no archive_url', checks['archive']['detail'])
        self.assertIn('host/service.json has no install.runtime_archive_url', checks['archive']['detail'])
        self.assertIn('is not hosted yet', checks['archive']['detail'])

    def test_hosted_archives_pass_and_a_single_archive_task_is_checked_as_before(self):
        url = 'https://huggingface.co/datasets/org/runtime/resolve/' + 'a' * 40 + '/toy/task.tar.gz'
        record = json.loads((self.bases / 'toy-base.json').read_text())
        record['archive_url'] = url.replace('task.tar.gz', 'base.tar.gz')
        (self.bases / 'toy-base.json').write_text(json.dumps(record))
        checks = self.checks(*self.repository(self.split_task, url))
        self.assertTrue(checks['archive']['ok'], checks['archive'])
        shutil.rmtree(self.root / 'repo')
        checks = self.checks(*self.repository(self.single_task, url))
        self.assertEqual((checks['manifest']['ok'], checks['archive']['ok']), (True, True))
        self.assertNotIn('base', checks['manifest']['detail'])

    def test_a_task_file_outside_the_task_paths_fails_the_manifest_check(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest['files']['third_party/extra.py'] = sha('code')
        task = task_tree(self.root / 'escaping-task', manifest, [])
        checks = self.checks(*self.repository(task))
        self.assertFalse(checks['manifest']['ok'])
        self.assertIn('outside the base', checks['manifest']['detail'])


if __name__ == '__main__':
    unittest.main()
