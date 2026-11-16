"""GPU lanes of the pool: configuration, parsing and the acceptance pick."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lanes  # noqa: E402


class LanesTest(unittest.TestCase):
    def test_default_is_all_four(self):
        self.assertEqual(lanes.lanes({}), (0, 1, 2, 3))
        self.assertEqual(lanes.lanes({'GPU_POOL_LANES': ''}), (0, 1, 2, 3))

    def test_configured(self):
        self.assertEqual(lanes.lanes({'GPU_POOL_LANES': '1, 2'}), (1, 2))
        for bad in ('4', '1,1', 'a', '-1', ','):
            with self.assertRaises(ValueError, msg=bad):
                lanes.lanes({'GPU_POOL_LANES': bad})

    def test_parse_gpu(self):
        env = {'GPU_POOL_LANES': '0,2'}
        self.assertEqual(lanes.parse_gpu('auto', env), 'auto')
        self.assertEqual(lanes.parse_gpu(None, env), 'auto')
        self.assertEqual(lanes.parse_gpu('2', env), 2)
        self.assertEqual(lanes.parse_gpu(0, env), 0)
        for bad in ('1', '3', 'x', '1.5'):
            with self.assertRaises(ValueError, msg=bad):
                lanes.parse_gpu(bad, env)

    def test_pick(self):
        for seed in range(20):
            got = lanes.pick(2, {}, random.Random(seed))
            self.assertEqual(len(set(got)), 2)
            self.assertTrue(set(got) <= {0, 1, 2, 3})
            self.assertEqual(got, sorted(got))
        self.assertEqual(lanes.pick(2, {'GPU_POOL_LANES': '3,1'}), [1, 3])
        with self.assertRaises(ValueError):
            lanes.pick(2, {'GPU_POOL_LANES': '1'})

    def test_cli(self):
        env = dict(os.environ, GPU_POOL_LANES='0,3')
        out = subprocess.run([sys.executable, str(HERE / 'lanes.py'), 'pick', '2'], env=env, check=True,
                             capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out), [0, 3])
        out = subprocess.run([sys.executable, str(HERE / 'lanes.py'), 'list'], env=env, check=True,
                             capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out), [0, 3])


if __name__ == '__main__':
    unittest.main()
