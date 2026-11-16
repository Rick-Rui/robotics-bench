import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parents[1] / 'remote'))
from client import Client, TransportError
from protocol import SCHEMA, digest, validate, validate_result

spec = importlib.util.spec_from_file_location('remote_server', Path(__file__).parents[1] / 'remote/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

REQUEST = {'workload': 'isaac-drop-v1', 'gpu': 0,
           'parameters': {'steps': 60, 'gravity': 9.81, 'initial_height': 1.0}}


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Condition did not become true')


class RemoteQueueTests(unittest.TestCase):
    def test_control_timeout_preserves_unknown_outcome_without_retry(self):
        client = Client('http://127.0.0.1:9', 'test-token', request_timeout=120)
        for error in (TimeoutError('socket stalled'), URLError(TimeoutError())):
            with self.subTest(error=type(error).__name__):
                with patch.object(client.opener, 'open', side_effect=error) as opened:
                    with self.assertRaises(TransportError) as caught:
                        client.submit(REQUEST, 'same-trial')
                    self.assertNotIsInstance(caught.exception, TimeoutError)
                    self.assertEqual(opened.call_count, 1)
                    self.assertEqual(opened.call_args.kwargs['timeout'], 120)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.locks = root / 'locks'
        self.locks.mkdir(mode=0o700)
        self.queue = server.Queue(root / 'jobs', self.locks, sys.executable, [0], executor=self.execute)
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, job, request, lock):
        self.entered.set()
        while not self.release.wait(0.02):
            if self.queue.cancelled(job):
                return 'cancelled', None, 'Cancelled'
        return 'succeeded', {'test_only': True}, None

    def tearDown(self):
        self.release.set()
        self.queue.close()
        self.temp.cleanup()

    def test_idempotency_owner_isolation_and_conflict(self):
        first = self.queue.submit('alice', 'retry', REQUEST)
        self.assertEqual(first['job_id'], self.queue.submit('alice', 'retry', REQUEST)['job_id'])
        changed = copy.deepcopy(REQUEST)
        changed['parameters']['gravity'] = 0
        with self.assertRaises(ValueError):
            self.queue.submit('alice', 'retry', changed)
        with self.assertRaises(KeyError):
            self.queue.get('bob', first['job_id'])
        self.assertNotEqual(first['job_id'], self.queue.submit('bob', 'retry', REQUEST)['job_id'])

    def test_anchor_lock_queues_job_then_cancel_releases_lane(self):
        lock = server.acquire_lock(self.locks, '0')
        job = self.queue.submit('owner', 'queued', REQUEST)['job_id']
        self.queue.start()
        time.sleep(0.7)
        self.assertEqual(self.queue.get('owner', job)['state'], 'queued')
        self.assertFalse(self.entered.is_set())
        os.close(lock)
        self.assertTrue(self.entered.wait(3))
        self.queue.cancel('owner', job)
        until(lambda: self.queue.get('owner', job)['state'] == 'cancelled')
        os.close(server.acquire_lock(self.locks, '0'))

    def test_queued_cancel_never_executes(self):
        job = self.queue.submit('owner', 'cancel', REQUEST)['job_id']
        self.queue.cancel('owner', job)
        self.queue.start()
        time.sleep(0.1)
        self.assertFalse(self.entered.is_set())
        self.assertEqual(self.queue.get('owner', job)['state'], 'cancelled')

    def test_restart_preserves_idempotency_and_fails_interrupted_job(self):
        job = self.queue.submit('owner', 'persist', REQUEST)['job_id']
        self.queue.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        self.queue.db.commit()
        self.queue.close()
        self.queue = server.Queue(Path(self.temp.name) / 'jobs', self.locks, sys.executable, [0])
        self.assertEqual(self.queue.submit('owner', 'persist', REQUEST)['job_id'], job)
        self.assertEqual(self.queue.get('owner', job)['state'], 'failed')

    def test_http_authentication_submit_and_cancel(self):
        token = 'test-only-' * 5
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.handler(self.queue, token))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(http.server_port)
        try:
            with self.assertRaises(HTTPError) as context:
                Client(url, 'wrong').request('/health')
            self.assertEqual(context.exception.code, 401)
            client = Client(url, token)
            job = client.submit(REQUEST, 'wire')['job_id']
            self.assertEqual(client.status(job)['state'], 'queued')
            self.assertEqual(client.log(job)['text'], '')
            self.assertEqual(client.cancel(job)['state'], 'cancelled')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_runtime_timeout_kills_process_and_produces_no_result(self):
        job = self.queue.submit('owner', 'timeout', REQUEST)['job_id']
        self.queue.timeout = 0.01
        real_popen = subprocess.Popen
        spawned = []
        def slow_runtime(*args, **kwargs):
            process = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 start_new_session=True)
            spawned.append(process)
            return process
        lock = server.acquire_lock(self.locks, '0')
        try:
            with patch.object(server.subprocess, 'check_output', return_value='0, GPU-test\n'), \
                    patch.object(server.subprocess, 'Popen', side_effect=slow_runtime):
                state, result, error = self.queue.execute(job, REQUEST, lock)
            self.assertEqual(state, 'timed_out')
            self.assertIsNone(result)
            self.assertIsNotNone(spawned[0].poll())
        finally:
            os.close(lock)

    def test_queue_deadline_prevents_late_execution(self):
        job = self.queue.submit('owner', 'stale', REQUEST)['job_id']
        self.queue.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time() - 601, job))
        self.queue.db.commit()
        self.queue.start()
        until(lambda: self.queue.get('owner', job)['state'] == 'timed_out')
        self.assertFalse(self.entered.is_set())


class RemoteContractTests(unittest.TestCase):
    def test_rejects_code_extra_fields_and_nonfinite_values(self):
        for field, value in [('workload', 'shell'), ('gpu', True), ('command', 'echo unsafe')]:
            bad = copy.deepcopy(REQUEST)
            bad[field] = value
            with self.assertRaises(ValueError):
                validate(bad)
        for value in [float('nan'), float('inf'), True, '9.81']:
            bad = copy.deepcopy(REQUEST)
            bad['parameters']['gravity'] = value
            with self.assertRaises(ValueError):
                validate(bad)

    def test_result_binds_request_and_recomputes_outcome(self):
        result = {'schema': SCHEMA, 'request_sha256': digest(REQUEST), 'status': 'passed',
                  'trajectory': [{'step': i, 'z': 0.1} for i in range(61)], 'score': 1.0, 'success': True}
        validate_result(result, REQUEST)
        for mutation in [{'request_sha256': 'stale'}, {'score': 0.0}, {'trajectory': []}]:
            with self.assertRaises(ValueError):
                validate_result(dict(result, **mutation), REQUEST)

    def test_process_group_cleanup_reaps_hanging_runtime(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        server.kill_group(process)
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
