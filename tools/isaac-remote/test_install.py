"""install.py follows host/service.json and reproduces the legacy install exactly."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install  # noqa: E402
import service as contract  # noqa: E402

CUROBO = 'robot_cfg:\n  asset_root_path: /root/autodl-tmp/LXC/RoboDojo/Assets/Robots/x5\n  urdf_path: /root/autodl-tmp/LXC/RoboDojo/Assets/Robots/x5/X5A.urdf\n'
LAUNCHER = 'import os\nlauncher_args["active_gpu"] = self.device_id\n'


def build(root, with_service):
    """A tiny task + runtime archive + engine tree shaped like the bimanual task."""
    task, engine = root/'task', root/'engine'
    files = {'Assets/Robots/x5/curobo.yml': CUROBO, 'Assets/Robots/x5/X5A.urdf': '<robot/>',
             'third_party/IsaacLab/source/isaaclab/isaaclab/app/app_launcher.py': LAUNCHER,
             'physical_rsi/sim.py': 'x = 1\n'}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, text in files.items():
            data = text.encode(); info = tarfile.TarInfo(name); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    (root/'runtime.tar.gz').write_bytes(buffer.getvalue())
    (engine/'kit/mdl').mkdir(parents=True); (engine/'kit/mdl/Base.mdl').write_text('mdl')
    manifest = {'archive_sha256': hashlib.sha256(buffer.getvalue()).hexdigest(),
                'files': {n: hashlib.sha256(t.encode()).hexdigest() for n, t in files.items()},
                'engine_assets': ['kit/mdl/Base.mdl'], 'layout': {'layout_id': 0},
                'action_limits': {'lower': [-1] * 14, 'upper': [1] * 14}}
    for d in ('environment/bundle', 'environment/runtime_common', 'tests/payload'):
        (task/d).mkdir(parents=True)
    (task/'environment/bundle/manifest.json').write_text(json.dumps(manifest))
    (task/'environment/runtime_common/protocol.py').write_text('V = 1\n')
    (task/'tests/payload/verify.py').write_text('V = 2\n')
    (task/'instruction.md').write_text('task\n')
    if with_service:
        (task/'host').mkdir()
        service = json.loads(json.dumps(contract.LEGACY_SERVICE))
        service['install']['registration']['case_id'] = 'toy-case'
        service['install']['registration']['native_action_limit'] = 500
        (task/'host/service.json').write_text(json.dumps(service))
    return task, engine


class InstallTests(unittest.TestCase):
    def test_legacy_task_installs_exactly_as_before(self):
        old = subprocess.run(['git', 'show', 'HEAD:tools/isaac-remote/install.py'], cwd=HERE,
                             capture_output=True, text=True)
        if old.returncode or 'contract.load_task' in old.stdout:
            self.skipTest('previous install.py is not available in this checkout')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); task, engine = build(root, with_service=False)
            (root/'old_install.py').write_text(old.stdout)
            outputs = {}
            # Same deployment path for both, since curobo.yml records the absolute runtime path.
            for name, script in (('old', root/'old_install.py'), ('new', HERE/'install.py')):
                subprocess.run([sys.executable, str(script), '--task', str(task), '--archive',
                                str(root/'runtime.tar.gz'), '--root', str(root/'deploy'), '--engine-root', str(engine)],
                               check=True, capture_output=True)
                outputs[name] = {n: (root/'deploy'/n).read_text() for n in (
                    'online-registration.json', 'replay-registration.json', 'migration.json',
                    'runtime/Assets/Robots/x5/curobo.yml',
                    'runtime/third_party/IsaacLab/source/isaaclab/isaaclab/app/app_launcher.py')}
                subprocess.run(['rm', '-rf', str(root/'deploy')], check=True)
            for name in outputs['old']:
                self.assertEqual(outputs['old'][name], outputs['new'][name], name)

    def test_declared_contract_is_installed_and_registered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); task, engine = build(root, with_service=True)
            install.install(task, root/'runtime.tar.gz', root/'deploy', engine)
            deploy = root/'deploy'
            self.assertTrue((deploy/'host/service.json').is_file())
            reg = json.loads((deploy/'online-registration.json').read_text())
            self.assertEqual((reg['case_id'], reg['native_action_limit']), ('toy-case', 500))
            migration = json.loads((deploy/'migration.json').read_text())
            self.assertIn('host/service.json', migration['installed_files'])
            self.assertEqual(migration['task_digest'], install.digest(task)[1])
            self.assertIn(str(deploy/'runtime/Assets/Robots/x5'),
                          (deploy/'runtime/Assets/Robots/x5/curobo.yml').read_text())
            # The task tree itself is untouched.
            self.assertEqual((task/'environment/runtime_common/protocol.py').read_text(), 'V = 1\n')

    def test_service_without_install_section_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); task, engine = build(root, with_service=True)
            service = json.loads((task/'host/service.json').read_text()); service.pop('install')
            (task/'host/service.json').write_text(json.dumps(service))
            with self.assertRaises(ValueError):
                install.install(task, root/'runtime.tar.gz', root/'deploy', engine)


if __name__ == '__main__':
    unittest.main()
