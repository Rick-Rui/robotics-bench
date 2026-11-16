"""Bounded CPU preparation -> remote GPU -> receipt pipeline with durable resume.

prepare(item) is trusted CPU-side integration code. It must return an immutable
registered request only AFTER its Docker resource has been stopped/released.
It must implement its own CPU/RAM limits and finite execution deadline. It never
receives the GPU service credential. A failed/ambiguous preparation is not
automatically repeated (it may have used a paid model API).
"""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import random
import time
from urllib.error import HTTPError, URLError
import uuid

from protocol import TERMINAL, canonical, digest, validate, validate_result
from scheduling import cpu_target, window


class Pipeline:
    def __init__(self, client, journal, items, prepare, cpu_slots=2, poll=1,
                 transport_timeout=60):
        if type(cpu_slots) is not int or cpu_slots < 1 or poll <= 0 or transport_timeout <= 0:
            raise ValueError('Invalid pipeline limits')
        if not items or any(not isinstance(item.get('id'), str) or not item['id'] for item in items):
            raise ValueError('Every item needs a nonempty string id')
        if len({item['id'] for item in items}) != len(items):
            raise ValueError('Duplicate item id')
        self.client, self.prepare = client, prepare
        self.path = Path(journal)
        self.cpu_slots, self.poll, self.transport_timeout = cpu_slots, poll, transport_timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.guard = self.path.with_suffix(self.path.suffix + '.lock').open('a')
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
                if self.data['manifest_sha256'] != digest(items):
                    raise ValueError('Resume manifest differs from the saved batch')
                for entry in self.data['items']:
                    if entry['state'] == 'preparing':
                        entry.update(state='failed', error='Interrupted CPU preparation; explicit new attempt required')
            else:
                self.data = {'schema': 'ziwu.cpu-gpu-pipeline.v1', 'batch_id': uuid.uuid4().hex,
                             'manifest_sha256': digest(items),
                             'items': [dict(item=item, state='pending') for item in items]}
            self.save()
        except BaseException:
            self.guard.close()
            raise

    def save(self):
        # All state mutations run on one asyncio thread; no callback writes state.
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        with temporary.open('wb') as file:
            file.write(canonical(self.data))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self):
        self.guard.close()

    async def io(self, function, *args):
        deadline, delay = time.monotonic() + self.transport_timeout, 0.25
        while True:
            try:
                async with self.network:
                    return await asyncio.to_thread(function, *args)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
                try:
                    delay = min(10, max(delay, float(exc.headers.get('Retry-After', delay))))
                except (ValueError, AttributeError):
                    pass
            except (URLError, OSError, TimeoutError):
                pass
            # Leave the request and its key resumable on prolonged disconnect.
            # Never turn an unknown remote outcome into a score or a new attempt.
            if time.monotonic() >= deadline:
                raise ConnectionError('Transport unavailable; resume this journal with the same manifest')
            await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
            delay = min(10, delay * 2)

    async def admit(self, entry):
        while True:
            async with self.admission:
                health = await self.io(self.client.request, '/health')
                preparing = sum(row['state'] == 'preparing' for row in self.data['items'])
                ready = sum(row['state'] == 'ready' for row in self.data['items'])
                samples = sorted((row for row in self.data['items'] if 'request' in row and 'cpu_finished' in row),
                                 key=lambda row: row['cpu_finished'])[-20:]
                cpu_seconds = (sum(row['cpu_finished'] - row['cpu_started'] for row in samples) / len(samples)
                               if samples else None)
                target = cpu_target(len(health['gpus']), self.cpu_slots, cpu_seconds, health['rollout_estimate_sec'])
                if window(len(health['gpus']), target, health['queued'],
                          health['running'], preparing, ready):
                    entry.update(state='preparing', cpu_started=time.time(),
                                 cpu_target=target, gpu_running_at_admission=health['running'],
                                 gpu_queued_at_admission=health['queued'])
                    self.save()
                    return
            await asyncio.sleep(self.poll)

    async def process(self, entry):
        if entry['state'] in TERMINAL:
            return
        async with self.inflight:
            if entry['state'] == 'pending':
                await self.admit(entry)
                try:
                    # The CPU slot is counted by state until callback cleanup ends.
                    request = await asyncio.to_thread(self.prepare, entry['item'])
                    validate(request)
                    entry.update(state='ready', request=request, cpu_finished=time.time())
                except Exception as exc:
                    entry.update(state='failed', error='CPU preparation: ' + str(exc), cpu_finished=time.time())
                self.save()
                if entry['state'] == 'failed':
                    return
            if entry['state'] == 'ready':
                # Saved BEFORE network submission. A lost response or process crash
                # repeats exactly this key and request, never a new GPU attempt.
                key = 'pipe-' + digest({'batch': self.data['batch_id'], 'item': entry['item']['id']})
                job = await self.io(self.client.submit, entry['request'], key)
                entry.update(state='submitted', job_id=job['job_id'])
                self.save()
            while entry['state'] == 'submitted':
                job = await self.io(self.client.status, entry['job_id'])
                if job['request_sha256'] != digest(entry['request']):
                    raise ValueError('Remote receipt does not match the frozen request')
                if job['state'] in TERMINAL:
                    if job['state'] == 'succeeded':
                        validate_result(job['result'], entry['request'])
                    entry.update(state=job['state'], remote=job, finished=time.time())
                    self.save()
                    return
                await asyncio.sleep(self.poll)

    async def run(self):
        self.network = asyncio.Semaphore(8)
        self.admission = asyncio.Lock()
        health = await self.io(self.client.request, '/health')
        if health.get('scheduler') != 'fair-pipeline-v1':
            raise ValueError('Deploy the fair pipeline GPU service before using this dispatcher')
        self.inflight = asyncio.Semaphore(3 * len(health['gpus']) + self.cpu_slots)
        async with asyncio.TaskGroup() as group:
            # Recover in-flight submissions first, so pending work cannot consume
            # all local permits while the remote jobs it depends on go unobserved.
            order = {'submitted': 0, 'ready': 1, 'pending': 2}
            for entry in sorted(self.data['items'], key=lambda row: order.get(row['state'], 3)):
                group.create_task(self.process(entry))
        return self.data
