"""host/service.json contract: validation, legacy equivalence and a CPU-only worker run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import service as contract  # noqa: E402


def old_commands(root, python, out, online_limit):
    """The commands the worker hard-coded before host/service.json existed."""
    runtime = root / 'runtime'
    online = out / 'online/session'
    serve = [python, str(root/'environment/supervise_service.py'),
             '--registration', str(root/'online-registration.json'),
             '--tool-profile', str(root/'environment/payload/tool-profile.json'),
             '--output', str(online), '--wall-limit', str(online_limit),
             '--headless', '--enable_cameras', '--experience',
             str(runtime/'third_party/IsaacLab/apps/isaaclab.python.headless.rendering.kit'),
             '--kit_args', '--enable isaacsim.replicator.behavior --enable isaacsim.sensors.camera '
             '--/app/extensions/registryEnabled=false']
    collect = [python, '-c', 'import sys; sys.path.insert(0, ' + repr(str(root/'environment')) + '); '
               'from collect_recording import collect; collect(' + repr(str(online)) + ', '
               + repr(str(out/'recording')) + ')']
    verify = [python, str(root/'tests/payload/verify.py'),
              '--registration', str(root/'replay-registration.json'),
              '--candidate', str(out/'recording/submission.json'), '--reference', str(out/'recording'),
              '--output', str(out/'verification'), '--python', python]
    pythonpath = [str(runtime), str(runtime/'XPolicyLab'),
                  str(runtime/'third_party/IsaacLab/source/isaaclab'),
                  str(runtime/'third_party/IsaacLab/source/isaaclab_assets'),
                  str(runtime/'third_party/IsaacLab/source/isaaclab_tasks'),
                  str(runtime/'third_party/curobo/src')]
    return serve, collect, verify, pythonpath


class ContractTests(unittest.TestCase):
    def test_legacy_contract_reproduces_the_original_worker(self):
        root, out = Path('/deploy/abc'), Path('/jobs/j1')
        values = dict(root=str(root), python='/py', online=str(out/'online/session'),
                      recording=str(out/'recording'), verification=str(out/'verification'),
                      online_limit='6600')
        serve, collect, verify, pythonpath = old_commands(root, '/py', out, 6600)
        legacy = contract.validate(contract.LEGACY_SERVICE)
        self.assertEqual(contract.command(legacy, 'serve', values), serve)
        self.assertEqual(contract.command(legacy, 'collect', values), collect)
        self.assertEqual(contract.command(legacy, 'verify', values), verify)
        self.assertEqual([str(root/p) for p in legacy['pythonpath']], pythonpath)
        self.assertEqual(contract.expand(legacy['serve']['ready_file'], values),
                         str(out/'online/session/endpoint.json'))

    def test_missing_file_means_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIs(contract.load(temp), contract.LEGACY_SERVICE)

    def test_rejects_host_paths_unknown_placeholders_and_escapes(self):
        good = json.loads(json.dumps(contract.LEGACY_SERVICE))
        for mutate in (lambda s: s['serve']['argv'].append('/root/autodl-tmp/x'),
                       lambda s: s['verify']['argv'].append('{secret}'),
                       lambda s: s.update(cwd='../outside'),
                       lambda s: s['pythonpath'].append('/opt/site'),
                       lambda s: s['collect'].update(timeout_sec=10**6),
                       lambda s: s.update(schema='other'),
                       lambda s: s.pop('verify'),
                       lambda s: s['serve'].update(ready_file='{online}/ready.flag')):
            bad = json.loads(json.dumps(good)); mutate(bad)
            with self.assertRaises(ValueError):
                contract.validate(bad)


SERVE = '''import json, sys, time
from pathlib import Path
online = Path(sys.argv[1]); online.mkdir(parents=True, exist_ok=True)
(online/'endpoint.json').write_text(json.dumps({'url': 'http://127.0.0.1:1', 'token': 't'}))
while not (online.parents[1]/'finish-command.json').exists():
    time.sleep(0.05)
(online/'actions.jsonl').write_text('[1]\\n')
'''
COLLECT = '''import shutil, sys
from pathlib import Path
out = Path(sys.argv[2]); out.mkdir(parents=True)
shutil.copy(Path(sys.argv[1])/'actions.jsonl', out/'actions.jsonl')
'''
VERIFY = '''import json, socket, sys
from pathlib import Path
out = Path(sys.argv[2]); out.mkdir(parents=True)
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM); network = 'allowed'
except OSError:
    network = 'denied'
actions = (Path(sys.argv[1])/'actions.jsonl').read_text().splitlines()
(out/'metrics.json').write_text(json.dumps({'status': 'complete', 'actions': len(actions), 'network': network}))
(out/'reward.txt').write_text('1')
'''


class WorkerRunTests(unittest.TestCase):
    """Run the real worker on a toy task that declares its own three phases."""

    def test_worker_follows_the_declared_phases(self):
        with tempfile.TemporaryDirectory() as temp:
            root, out = Path(temp)/'deploy', Path(temp)/'job'
            (root/'host').mkdir(parents=True)
            for name, text in (('serve.py', SERVE), ('collect.py', COLLECT), ('verify.py', VERIFY)):
                (root/'host'/name).write_text(text)
            service = {'schema': contract.SCHEMA, 'cwd': 'host', 'pythonpath': ['host'],
                       'serve': {'argv': ['{python}', '{root}/host/serve.py', '{online}'],
                                 'ready_file': '{online}/endpoint.json', 'startup_timeout_sec': 60},
                       'collect': {'argv': ['{python}', '{root}/host/collect.py', '{online}', '{recording}'],
                                   'timeout_sec': 60},
                       'verify': {'argv': ['{python}', '{root}/host/verify.py', '{recording}', '{verification}'],
                                  'timeout_sec': 60}}
            (root/'host/service.json').write_text(json.dumps(service))
            files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in root.rglob('*') if p.is_file()}
            (root/'migration.json').write_text(json.dumps({'installed_files': files}))
            env = dict(os.environ, ZIWU_GPU_INDEX='1')
            worker = subprocess.Popen([sys.executable, str(HERE/'worker.py'), '--deployment', str(root),
                                       '--directory', str(out), '--python', sys.executable,
                                       '--online-limit', '60'], env=env)
            try:
                deadline = time.monotonic() + 30
                while not (out/'online/session/endpoint.json').exists():
                    self.assertIsNone(worker.poll(), 'worker exited before the session was ready')
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)
                (out/'finish-command.json').write_text('{"finish": true}\n')
                self.assertEqual(worker.wait(timeout=60), 0)
            finally:
                if worker.poll() is None:
                    worker.kill()
            state = json.loads((out/'session-state.json').read_text())
            self.assertEqual(state['state'], 'complete')
            self.assertEqual(state['reward'], 1)
            self.assertEqual(state['metrics']['actions'], 1)
            self.assertEqual(state['metrics']['network'], 'denied')

    def test_changed_installed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'deploy'; (root/'host').mkdir(parents=True)
            (root/'host/service.json').write_text(json.dumps(contract.LEGACY_SERVICE))
            (root/'migration.json').write_text(json.dumps({'installed_files': {'host/service.json': '0'*64}}))
            result = subprocess.run([sys.executable, str(HERE/'worker.py'), '--deployment', str(root),
                                     '--directory', str(Path(temp)/'job'), '--python', sys.executable],
                                    env=dict(os.environ, ZIWU_GPU_INDEX='1'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Registered task adapter changed', result.stderr)


if __name__ == '__main__':
    unittest.main()
