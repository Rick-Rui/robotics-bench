import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from test_remote import Client, REQUEST, SCHEMA, digest, server, until
from pipeline import Pipeline
from scheduling import choose, cpu_target, window


def request(trial, phase='final', gpu='auto'):
    value = copy.deepcopy(REQUEST)
    value.update(gpu=gpu, scheduling={'trial': trial, 'phase': phase})
    return value


def receipt(req):
    score = float(req['parameters']['gravity'] != 0)
    return {'schema': SCHEMA, 'request_sha256': digest(req), 'status': 'passed',
            'trajectory': [{'step': i, 'z': 0.1 if score else 1.0}
                           for i in range(req['parameters']['steps'] + 1)],
            'score': score, 'success': bool(score)}


class SchedulingTests(unittest.TestCase):
    def test_fair_service_phase_share_aging_and_affinity(self):
        def row(name, phase, created=100, gpu='auto'):
            return dict(id=name, owner='owner', created=created, request=request(name, phase, gpu))
        busy = row('busy', 'feedback')
        fresh = row('fresh', 'feedback', 101)
        final = row('final', 'final', 102)
        service = {('owner', 'busy'): 600, ('owner', 'fresh'): 10}
        self.assertEqual(choose([busy, fresh, final], 0, service, [], 110)['id'], 'fresh')
        self.assertEqual(choose([busy, fresh, final], 0, service, ['feedback'] * 3, 110)['id'], 'final')
        self.assertEqual(choose([busy, fresh, final], 0, service, [], 230)['id'], 'busy')
        self.assertIsNone(choose([row('pinned', 'final', gpu=0)], 1, {}, [], 110))

    def test_backpressure_counts_cpu_and_gpu_work(self):
        self.assertEqual(window(1, 4, queued=0, running=1, preparing=1), 3)
        self.assertEqual(window(1, 4, queued=2, running=1, preparing=0), 0)
        self.assertEqual(window(2, 2, queued=0, running=2, preparing=2), 0)
        self.assertEqual(cpu_target(1, 4, 100, 10), 4)
        self.assertEqual(cpu_target(1, 4, 1, 100), 2)


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.locks = self.root / 'locks'
        self.locks.mkdir(mode=0o700)
        self.entered, self.release = threading.Event(), threading.Event()
        self.executions = []
        self.queue = server.Queue(self.root / 'gpu', self.locks, sys.executable, [0, 1], executor=self.execute)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.handler(self.queue, 'test-token'))
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.client = Client('http://127.0.0.1:' + str(self.http.server_port), 'test-token')
        self.batch = None

    def execute(self, job_id, req, lock):
        self.executions.append(job_id)
        self.entered.set()
        while not self.release.wait(0.01):
            if self.queue.cancelled(job_id):
                return 'cancelled', None, 'Cancelled'
        return 'succeeded', receipt(req), None

    def tearDown(self):
        self.release.set()
        if self.batch:
            self.batch.close()
        self.queue.close()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join()
        self.temp.cleanup()

    def test_free_gpu_steals_auto_work_when_anchor_owns_other_card(self):
        lock = server.acquire_lock(self.locks, '0')
        try:
            pinned = self.queue.submit('owner', 'pinned', request('pinned', gpu=0))
            auto = self.queue.submit('owner', 'auto', request('auto'))
            self.queue.start()
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.queue.get('owner', pinned['job_id'])['state'], 'queued')
            self.assertEqual(self.queue.get('owner', auto['job_id'])['assigned_gpu'], 1)
            self.release.set()
            until(lambda: self.queue.get('owner', auto['job_id'])['state'] == 'succeeded')
            self.assertEqual(self.queue.get('owner', auto['job_id'])['request_sha256'], digest(request('auto')))
        finally:
            os.close(lock)

    def test_http_backpressure_is_retryable_and_existing_key_still_works(self):
        jobs = [self.client.submit(request('one-trial'), 'key-' + str(i)) for i in range(4)]
        with self.assertRaises(HTTPError) as context:
            self.client.submit(request('one-trial'), 'overflow')
        self.assertEqual(context.exception.code, 429)
        self.assertEqual(context.exception.headers['Retry-After'], '2')
        self.assertEqual(self.client.submit(request('one-trial'), 'key-0')['job_id'], jobs[0]['job_id'])
        self.assertEqual(self.client.request('/health')['queued'], 4)

    def test_dispatch_reservation_and_actual_service_survive_restart(self):
        job = self.queue.submit('owner', 'service', request('trial'))['job_id']
        self.queue.start()
        self.assertTrue(self.entered.wait(3))
        self.assertEqual(self.queue.get('owner', job)['gpu_seconds'], 30)
        self.release.set()
        until(lambda: self.queue.get('owner', job)['state'] == 'succeeded')
        elapsed = self.queue.get('owner', job)['gpu_seconds']
        self.assertGreater(elapsed, 0)
        self.assertLess(elapsed, 30)
        self.queue.close()
        self.queue = server.Queue(self.root / 'gpu', self.locks, sys.executable, [0, 1], executor=self.execute)
        self.assertEqual(self.queue.get('owner', job)['gpu_seconds'], elapsed)
        self.assertIsNotNone(self.queue.get('owner', job)['assigned_gpu'])

    def test_cpu_preparation_overlaps_gpu_and_window_bounds_resident_work(self):
        self.queue.gpus = [0]
        self.queue.start()
        prepared, mutex = [], threading.Lock()
        active, peak = 0, 0
        def prepare(item):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with mutex:
                prepared.append(item['id'])
                active -= 1
            return request(item['id'])
        items = [{'id': str(i)} for i in range(8)]
        self.batch = Pipeline(self.client, self.root / 'batch.json', items, prepare, cpu_slots=2, poll=0.02)
        async def exercise():
            task = asyncio.create_task(self.batch.run())
            try:
                deadline = time.monotonic() + 5
                while len(prepared) < 3 and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertTrue(self.entered.is_set())
                self.assertGreaterEqual(len(prepared), 3)
                # Two prefetched rollouts plus already-admitted CPU work is
                # bounded even when the GPU stops completing work.
                await asyncio.sleep(0.1)
                self.assertLessEqual(len(prepared), 5)
                self.assertLessEqual(peak, 2)
                self.release.set()
                result = await asyncio.wait_for(task, 10)
                self.assertTrue(all(row['state'] == 'succeeded' for row in result['items']))
                self.assertEqual(len(self.executions), 8)
            finally:
                self.release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        asyncio.run(exercise())

    def test_lost_submit_reply_resume_does_not_repeat_cpu_or_gpu(self):
        items = [{'id': 'resume'}]
        prepare = lambda item: request(item['id'])
        self.batch = Pipeline(self.client, self.root / 'batch.json', items, prepare, poll=0.01)
        real_submit = self.client.submit
        def lost(req, key):
            real_submit(req, key)
            # A fatal transport interruption leaves the saved request at ready.
            raise RuntimeError('simulate crash after acceptance, before receipt')
        with patch.object(self.client, 'submit', side_effect=lost):
            with self.assertRaises(ExceptionGroup):
                asyncio.run(self.batch.run())
        self.assertEqual(self.queue.health()['queued'], 1)
        self.batch.close()
        self.batch = Pipeline(self.client, self.root / 'batch.json', items,
                              lambda _: self.fail('Preparation repeated on resume'), poll=0.01)
        self.queue.start()
        self.release.set()
        result = asyncio.run(self.batch.run())
        self.assertEqual(result['items'][0]['state'], 'succeeded')
        self.assertEqual(len(self.executions), 1)

    def test_ambiguous_cpu_is_not_replayed_and_manifest_is_bound(self):
        items = [{'id': 'paid-model'}]
        path = self.root / 'batch.json'
        self.batch = Pipeline(self.client, path, items, lambda _: REQUEST)
        self.batch.data['items'][0]['state'] = 'preparing'
        self.batch.save()
        with self.assertRaises(BlockingIOError):
            Pipeline(self.client, path, items, lambda _: REQUEST)
        self.batch.close()
        self.batch = Pipeline(self.client, path, items, lambda _: self.fail('Repeated model call'))
        self.assertEqual(asyncio.run(self.batch.run())['items'][0]['state'], 'failed')
        self.batch.close()
        with self.assertRaises(ValueError):
            Pipeline(self.client, path, [{'id': 'different'}], lambda _: REQUEST)

    def test_transient_connection_failure_and_zero_score_remain_distinct(self):
        req = request('zero')
        req['parameters']['gravity'] = 0
        self.batch = Pipeline(self.client, self.root / 'batch.json', [{'id': 'zero'}], lambda _: req, poll=0.01)
        real_submit = self.client.submit
        calls = 0
        def flaky(value, key):
            nonlocal calls
            calls += 1
            job = real_submit(value, key)
            if calls == 1:
                raise URLError('dropped response')
            return job
        self.release.set()
        self.queue.start()
        with patch.object(self.client, 'submit', side_effect=flaky):
            result = asyncio.run(self.batch.run())
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(result['items'][0]['state'], 'succeeded')
        self.assertEqual(result['items'][0]['remote']['result']['score'], 0)


if __name__ == '__main__':
    unittest.main()
