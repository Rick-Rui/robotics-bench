import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('probe', Path(__file__).parents[1] / 'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LaneLockTests(unittest.TestCase):
    def test_anchor_lock_excludes_same_gpu_but_not_other_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            first = probe.acquire_lock(directory, '0')
            try:
                with self.assertRaises(BlockingIOError):
                    probe.acquire_lock(directory, '0')
                second = probe.acquire_lock(directory, '1')
                os.close(second)
                self.assertTrue((Path(directory) / 'anchor-robodojo-gpu-0.lock').exists())
            finally:
                os.close(first)
            os.close(probe.acquire_lock(directory, '0'))

    def test_symlink_and_shared_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'link'
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(ValueError):
                probe.acquire_lock(link, '0')
            os.chmod(directory, 0o755)
            with self.assertRaises(ValueError):
                probe.acquire_lock(directory, '0')

    def test_lock_file_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'anchor-robodojo-gpu-0.lock').symlink_to('/dev/null')
            with self.assertRaises(OSError):
                probe.acquire_lock(directory, '0')

    def test_bad_gpu_rejected_before_output_created(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'out'
            result = subprocess.run([sys.executable, probe.__file__, '--gpu', '4',
                                     '--output', str(target)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_timeout_reaps_child_before_returning(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        original = process.wait
        calls = [0]
        def wait(timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                raise subprocess.TimeoutExpired('probe', timeout)
            return original(timeout=timeout)
        try:
            with patch.object(probe.subprocess, 'Popen', return_value=process), patch.object(process, 'wait', wait):
                with self.assertRaises(subprocess.TimeoutExpired):
                    probe.run_runtime([], os.environ, subprocess.DEVNULL)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                original()


if __name__ == '__main__':
    unittest.main()
