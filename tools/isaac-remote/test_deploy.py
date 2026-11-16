"""/validate deployment: git archive -> host POST /deployments -> install.py, end to end on CPU."""
from functools import partial
import gzip
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'anchor-gpu/remote'))
import server  # noqa: E402
sys.path.insert(0, str(HERE))
import deploy_task  # noqa: E402
import install  # noqa: E402
import validate_report  # noqa: E402
from test_install import build  # noqa: E402

TOKEN = 't' * 40
TASK_PATH = 'tasks/demo/toy-gpu-task'
REVISION = 'a' * 40
HF_PATH = f'/datasets/org/runtime/resolve/{REVISION}/toy/runtime.tar.gz'


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        task, self.engine = build(root, with_service=True)
        service = json.loads((task / 'host/service.json').read_text())
        service['install']['runtime_archive_url'] = 'https://huggingface.co' + HF_PATH
        (task / 'host/service.json').write_text(json.dumps(service))
        # A one-commit repository holding the task at TASK_PATH.
        self.repo = root / 'repo'
        (self.repo / TASK_PATH).parent.mkdir(parents=True)
        task.rename(self.repo / TASK_PATH)
        git(self.repo, 'init', '-q')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'task')
        self.sha = git(self.repo, 'rev-parse', 'HEAD')
        # The "Hugging Face mirror": serves the runtime archive at the Hub path.
        mirror = root / 'mirror' / HF_PATH.lstrip('/')
        mirror.parent.mkdir(parents=True)
        (root / 'runtime.tar.gz').rename(mirror)
        self.mirror = ThreadingHTTPServer(('127.0.0.1', 0), partial(_Quiet, directory=str(root / 'mirror')))
        threading.Thread(target=self.mirror.serve_forever, daemon=True).start()
        self.env = {'ZIWU_TASK_DEPLOYMENTS': str(root / 'deployments'), 'ZIWU_ENGINE_ROOT': str(self.engine),
                    'ZIWU_RUNTIME_CACHE': str(root / 'cache'),
                    'ZIWU_HF_ENDPOINT': f'http://127.0.0.1:{self.mirror.server_port}'}
        with patch.dict(os.environ, self.env):
            self.queue = server.Queue(root / 'jobs', root / 'locks', sys.executable, [1, 2])
            deployer = server.Deployer(root / 'jobs', sys.executable, installer=HERE / 'install.py')
            self.host = ThreadingHTTPServer(('127.0.0.1', 0), server.handler(self.queue, TOKEN, deployer))
        threading.Thread(target=self.host.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.host.server_port}'

    def tearDown(self):
        self.host.shutdown(); self.host.server_close()
        self.mirror.shutdown(); self.mirror.server_close()
        self.queue.close()
        self.temp.cleanup()

    def deploy(self):
        with patch.dict(os.environ, self.env):
            return deploy_task.deploy(self.repo, self.sha, TASK_PATH, self.url, TOKEN, timeout=60, poll=0.2,
                                      log=lambda *_: None)

    def test_validate_installs_the_task_once_and_the_host_finds_it(self):
        state = self.deploy()
        self.assertEqual(state['state'], 'installed', state.get('log_tail'))
        digest = state['task_digest']
        with patch.dict(os.environ, self.env):
            deployment = server.task_deployment(digest)
        self.assertEqual(deployment, self.root / 'deployments' / digest)
        self.assertIn('"runtime_archive": "downloaded"', state['log_tail'])
        # The runtime archive is now cached under its sha256; a repeat is a no-op.
        self.assertEqual(len(list((self.root / 'cache').glob('*.tar.gz'))), 1)
        again = self.deploy()
        self.assertEqual((again['state'], again['task_digest']), ('installed', digest))

    def test_digest_mismatch_and_tampered_archive_are_refused(self):
        with patch.dict(os.environ, self.env):
            raw = subprocess.check_output(['git', 'archive', self.sha + ':' + TASK_PATH], cwd=self.repo)
            bundle = self.root / 'bundle.tar.gz'
            bundle.write_bytes(gzip.compress(raw))
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                install.install_bundle(bundle, 'b' * 64, self.root / 'deployments', self.root / 'cache',
                                       self.engine, self.env['ZIWU_HF_ENDPOINT'])
            mirror = self.root / 'mirror' / HF_PATH.lstrip('/')
            mirror.write_bytes(mirror.read_bytes() + b'x')
            state = self.deploy()
        self.assertEqual(state['state'], 'failed')
        self.assertIn('does not match archive_sha256', state['log_tail'])
        self.assertFalse((self.root / 'deployments' / state['task_digest']).exists())
        self.assertEqual(list((self.root / 'cache').glob('*')), [])

    def test_unsafe_bundle_members_are_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            link = tarfile.TarInfo('environment/link'); link.type = tarfile.SYMTYPE; link.linkname = '/etc/passwd'
            archive.addfile(link)
        bundle = self.root / 'evil.tar.gz'
        bundle.write_bytes(buffer.getvalue())
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            install.unpack_bundle(bundle, self.root / 'evil')

    def test_runtime_url_must_be_a_pinned_hub_file(self):
        import service as contract
        spec = json.loads((self.repo / TASK_PATH / 'host/service.json').read_text())
        for url in ('https://example.com/runtime.tar.gz',
                    'https://huggingface.co/datasets/org/runtime/resolve/main/runtime.tar.gz'):
            spec['install']['runtime_archive_url'] = url
            with self.assertRaises(ValueError):
                contract.validate(spec)

    def test_host_without_deploy_settings_refuses(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server.Deployer.enabled())
            with self.assertRaisesRegex(ValueError, 'not enabled'):
                server.Deployer(self.root / 'jobs', sys.executable).submit('a' * 64, b'x')


class ReportTests(unittest.TestCase):
    def test_undeployed_task_fails_the_gate_without_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            result, markdown, record = validate_report.report(
                TASK_PATH, 'tree', True, {'state': 'failed', 'error': 'Runtime archive missing'}, Path(temp))
        self.assertEqual((result['oracle'], result['nop'], record), ('', '', None))
        self.assertIn('Runtime archive missing', markdown)

    def test_missing_control_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            result, markdown, record = validate_report.report(
                TASK_PATH, 'tree', True, {'state': 'installed', 'task_digest': 'a' * 64}, Path(temp))
        self.assertEqual((result['oracle'], result['nop'], record), ('false', 'false', None))
        self.assertIn('not run', markdown)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == '__main__':
    unittest.main()
