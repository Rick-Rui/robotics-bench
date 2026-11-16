"""Loopback-only asynchronous GPU queue; expose through an authenticated SSH tunnel."""
import argparse
import csv
import fcntl
import gzip
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error

from protocol import SCHEMA, TERMINAL, canonical, digest, validate, validate_result
from scheduling import choose, metadata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe import acquire_lock



def task_deployment(digest):
    """Return the installed deployment for one task digest.

    ZIWU_TASK_DEPLOYMENTS holds one directory per installed task digest, so a
    new task revision is installed next to the old one and in-flight sessions
    keep their own files. ZIWU_BIMANUAL_DEPLOYMENT (a single deployment) is
    still accepted.
    """
    root = os.environ.get('ZIWU_TASK_DEPLOYMENTS')
    candidates = [Path(root) / digest] if root else []
    if os.environ.get('ZIWU_BIMANUAL_DEPLOYMENT'):
        candidates.append(Path(os.environ['ZIWU_BIMANUAL_DEPLOYMENT']))
    for path in candidates:
        migration = path / 'migration.json'
        if migration.is_file() and json.loads(migration.read_text())['task_digest'] == digest:
            return path
    raise ValueError('Requested task is not deployed on this host')


def task_runtime_enabled():
    return bool(os.environ.get('ZIWU_TASK_DEPLOYMENTS') or os.environ.get('ZIWU_BIMANUAL_DEPLOYMENT'))


class Deployer:
    """Install task trees sent by /validate into ZIWU_TASK_DEPLOYMENTS/<digest>.

    The server only stores the bundle and runs the platform installer
    (install.py, shipped with this release) in a subprocess; it never executes
    task code. One installation runs at a time; repeated requests for the same
    digest are idempotent.
    """
    LIMIT = 32 * 1024**2
    TIMEOUT = 3600

    def __init__(self, root, python, installer=None):
        self.root = Path(root) / 'installs'
        self.python = python
        # install.py ships next to server.py in a host release (deploy.py).
        self.installer = Path(installer) if installer else Path(__file__).with_name('install.py')
        self.lock = threading.Lock()
        self.active = {}

    @staticmethod
    def enabled():
        return bool(os.environ.get('ZIWU_TASK_DEPLOYMENTS') and os.environ.get('ZIWU_ENGINE_ROOT')
                    and os.environ.get('ZIWU_RUNTIME_CACHE'))

    def directory(self, task_digest):
        if not re.fullmatch(r'[0-9a-f]{64}', task_digest):
            raise ValueError('Invalid task digest')
        return self.root / task_digest

    def status(self, task_digest):
        directory = self.directory(task_digest)
        try:
            task_deployment(task_digest)
            installed = True
        except (ValueError, OSError, KeyError):
            installed = False
        with self.lock:
            running = task_digest in self.active
        if running:
            state = {'state': 'installing'}
        elif installed:
            state = {'state': 'installed'}
        elif (directory / 'state.json').is_file():
            state = json.loads((directory / 'state.json').read_text())
        else:
            state = {'state': 'absent'}
        log = directory / 'install.log'
        if log.is_file():
            state['log_tail'] = log.read_bytes()[-8192:].decode('utf-8', errors='replace')
        state['task_digest'] = task_digest
        return state

    def submit(self, task_digest, bundle):
        if not self.enabled():
            raise ValueError('Automatic task deployment is not enabled on this host')
        if not 0 < len(bundle) <= self.LIMIT:
            raise ValueError('Invalid task bundle size')
        directory = self.directory(task_digest)
        current = self.status(task_digest)
        if current['state'] in ('installed', 'installing'):
            return current
        with self.lock:
            if task_digest in self.active:
                return {'state': 'installing', 'task_digest': task_digest}
            if self.active:
                raise QueueFull('Another task installation is running')
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (directory / 'state.json').unlink(missing_ok=True)
            (directory / 'bundle.tar.gz').write_bytes(bundle)
            thread = threading.Thread(target=self.run, args=(task_digest,), daemon=True)
            self.active[task_digest] = thread
            thread.start()
        return {'state': 'installing', 'task_digest': task_digest}

    def run(self, task_digest):
        directory = self.directory(task_digest)
        command = [self.python, str(self.installer),
                   '--bundle', str(directory / 'bundle.tar.gz'), '--expect-digest', task_digest,
                   '--deployments', os.environ['ZIWU_TASK_DEPLOYMENTS'],
                   '--runtime-cache', os.environ['ZIWU_RUNTIME_CACHE'],
                   '--engine-root', os.environ['ZIWU_ENGINE_ROOT']]
        if os.environ.get('ZIWU_HF_ENDPOINT'):
            command += ['--hf-endpoint', os.environ['ZIWU_HF_ENDPOINT']]
        allowed = {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL'}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        state = {'state': 'failed'}
        try:
            with (directory / 'install.log').open('w') as log:
                process = subprocess.Popen(command, cwd=directory, env=env, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    process.wait(timeout=self.TIMEOUT)
                except subprocess.TimeoutExpired:
                    kill_group(process)
                    state['error'] = 'Installation timed out'
            if process.returncode == 0:
                state = {'state': 'installed'}
            elif 'error' not in state:
                state['error'] = 'Installer exited with code {}'.format(process.returncode)
        except Exception as exc:
            state['error'] = '{}: {}'.format(type(exc).__name__, exc)
        finally:
            (directory / 'bundle.tar.gz').unlink(missing_ok=True)
            (directory / 'state.json').write_text(json.dumps(state) + '\n')
            with self.lock:
                self.active.pop(task_digest, None)

class QueueFull(ValueError):
    """Transient backpressure; retry the SAME idempotency key."""


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    except ProcessLookupError:
        process.wait()


class Queue:
    def __init__(self, root, lock_dir, python, gpus, timeout=300, executor=None):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.guard = (self.root / 'server.lock').open('w')
        fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock_dir, self.python = str(lock_dir), python
        self.gpus, self.timeout = gpus, timeout
        self.executor = executor or self.execute
        self.db = sqlite3.connect(self.root / 'jobs.sqlite3', check_same_thread=False)
        self.mutex = threading.RLock()
        self.changed = threading.Condition(self.mutex)
        self.stop = threading.Event()
        self.threads = []
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, '
                        'key TEXT, hash TEXT, request TEXT, state TEXT, cancel INTEGER DEFAULT 0, '
                        'created REAL, updated REAL, result TEXT, error TEXT, UNIQUE(owner,key))')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, kind in [('assigned_gpu', 'INTEGER'), ('started', 'REAL'),
                           ('gpu_seconds', 'REAL DEFAULT 0')]:
            if name not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + kind)
        # Never automatically replay an interrupted simulation after a service restart.
        self.db.execute("UPDATE jobs SET state='failed', error='Service restarted during execution', "
                        "updated=? WHERE state='running'", (time.time(),))
        self.db.commit()

    def start(self):
        for gpu in self.gpus:
            thread = threading.Thread(target=self.work, args=(gpu,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        with self.changed:
            self.changed.notify_all()
        for thread in self.threads:
            thread.join()
        self.db.close()
        self.guard.close()

    def submit(self, owner, key, request):
        validate(request)
        if request['workload'] == 'bimanual-session-v1':
            task_deployment(request['parameters']['task_digest'])
        if request['gpu'] != 'auto' and request['gpu'] not in self.gpus:
            raise ValueError('GPU is not enabled on this service')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', key):
            raise ValueError('Invalid Idempotency-Key')
        hashed = digest(request)
        with self.mutex:
            row = self.db.execute('SELECT id,hash FROM jobs WHERE owner=? AND key=?', (owner, key)).fetchone()
            if row:
                if row[1] != hashed:
                    raise ValueError('Idempotency key already used for a different request')
                return self.get(owner, row[0])
            count = self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if count >= 32:
                raise QueueFull('Queue full')
            trial = metadata(request)['trial']
            if 'scheduling' in request:
                active = self.db.execute("SELECT request FROM jobs WHERE owner=? AND state IN ('queued','running')", (owner,)).fetchall()
                if sum(metadata(json.loads(row[0]))['trial'] == trial for row in active) >= 4:
                    raise QueueFull('Trial already has four outstanding rollouts')
            job_id, now = uuid.uuid4().hex, time.time()
            self.db.execute('INSERT INTO jobs(id,owner,key,hash,request,state,created,updated) '
                            "VALUES(?,?,?,?,?,'queued',?,?)",
                            (job_id, owner, key, hashed, canonical(request).decode(), now, now))
            self.db.commit()
            self.changed.notify_all()
            return self.get(owner, job_id)

    def get(self, owner, job_id):
        with self.mutex:
            row = self.db.execute('SELECT id,hash,request,state,created,updated,result,error '
                                  ',assigned_gpu,started,gpu_seconds FROM jobs WHERE owner=? AND id=?', (owner, job_id)).fetchone()
        if not row:
            raise KeyError('Unknown job')
        return {'schema': SCHEMA, 'job_id': row[0], 'request_sha256': row[1],
                'request': json.loads(row[2]), 'state': row[3], 'created': row[4], 'updated': row[5],
                'result': json.loads(row[6]) if row[6] else None, 'error': row[7],
                'assigned_gpu': row[8], 'started': row[9],
                'gpu_seconds': row[10],
                'queue_seconds': max(0, (row[9] or (row[5] if row[3] in TERMINAL else time.time())) - row[4])}

    def cancel(self, owner, job_id):
        with self.mutex:
            job = self.get(owner, job_id)
            if job['state'] not in TERMINAL:
                state = 'cancelled' if job['state'] == 'queued' else job['state']
                self.db.execute('UPDATE jobs SET cancel=1,state=?,updated=? WHERE id=?',
                                (state, time.time(), job_id))
                self.db.commit()
                self.changed.notify_all()
            return self.get(owner, job_id)

    def cancelled(self, job_id):
        with self.mutex:
            return self.stop.is_set() or bool(self.db.execute('SELECT cancel FROM jobs WHERE id=?', (job_id,)).fetchone()[0])

    def log(self, owner, job_id, offset=0):
        job = self.get(owner, job_id)
        path = self.root / job_id / 'runtime.log'
        data = b''
        if path.exists():
            with path.open('rb') as file:
                file.seek(offset)
                data = file.read(65536)
        return {'job_id': job_id, 'offset': offset, 'next_offset': offset + len(data),
                'state': job['state'], 'text': data.decode('utf-8', errors='replace')}

    def finish(self, job_id, state, result=None, error=None, elapsed=None):
        with self.mutex:
            # A cancellation arriving between child exit and publication still wins.
            if self.cancelled(job_id):
                state, result, error = 'cancelled', None, 'Cancelled; process group reaped'
            self.db.execute('UPDATE jobs SET state=?,result=?,error=?,updated=?,gpu_seconds=COALESCE(?,gpu_seconds) WHERE id=?',
                            (state, canonical(result).decode() if result else None, error, time.time(), elapsed, job_id))
            self.db.commit()
            self.changed.notify_all()

    def estimate(self):
        samples = self.db.execute("SELECT gpu_seconds FROM jobs WHERE state='succeeded' AND started IS NOT NULL ORDER BY updated DESC LIMIT 20").fetchall()
        return max(1, sum(row[0] for row in samples) / len(samples)) if samples else 30.0

    def scheduling_rows(self):
        return [dict(id=row[0], owner=row[1], request=json.loads(row[2]), state=row[3],
                     created=row[4], started=row[5], gpu_seconds=row[6], assigned_gpu=row[7])
                for row in self.db.execute('SELECT id,owner,request,state,created,started,gpu_seconds,assigned_gpu FROM jobs')]

    def health(self):
        with self.mutex:
            rows = self.scheduling_rows()
            active = [row for row in rows if row['state'] in ('queued', 'running')]
            queued = sum(row['state'] == 'queued' for row in active)
            running = len(active) - queued
            estimate = self.estimate()
            return {'schema': SCHEMA, 'gpus': self.gpus, 'workloads': ['isaac-drop-v1'] +
                    (['bimanual-session-v1'] if task_runtime_enabled() else []),
                    'task_deployment': Deployer.enabled(),
                    'scheduler': 'fair-pipeline-v1', 'queued': queued, 'running': running,
                    'queue_capacity': 32, 'per_trial_capacity': 4,
                    'rollout_estimate_sec': estimate,
                    'prefetch_capacity': 3 * len(self.gpus),
                    'preparation_credits': max(0, 3 * len(self.gpus) - len(active)),
                    'phases': {phase: sum(metadata(row['request'])['phase'] == phase for row in active)
                               for phase in ('feedback', 'final')},
                    'lanes': [{'gpu': gpu, 'running_job': next((row['id'] for row in active
                               if row['state'] == 'running' and row['assigned_gpu'] == gpu), None)}
                              for gpu in self.gpus]}

    def work(self, gpu):
        while not self.stop.is_set():
            row = None
            lock = None
            with self.mutex:
                self.db.execute("UPDATE jobs SET state='timed_out',error='Queue wait expired',updated=? "
                                "WHERE state='queued' AND created<?", (time.time(), time.time() - 600))
                self.db.commit()
                rows = self.scheduling_rows()
                service = {}
                for item in rows:
                    group = (item['owner'], metadata(item['request'])['trial'])
                    service[group] = service.get(group, 0) + item['gpu_seconds']
                recent = sorted((item for item in rows if item['started'] is not None),
                                key=lambda item: item['started'], reverse=True)
                row = choose([item for item in rows if item['state'] == 'queued'], gpu, service,
                             [metadata(item['request'])['phase'] for item in recent[:3]], time.time())
                if row:
                    try:
                        lock = acquire_lock(self.lock_dir, str(gpu))
                    except BlockingIOError:
                        row = None  # Anchor owns this card; retain queued state.
                    except Exception as exc:
                        self.finish(row['id'], 'failed', error=str(exc))
                        row = None
                    if row:
                        now = time.time()
                        self.db.execute("UPDATE jobs SET state='running',updated=?,started=?,assigned_gpu=?,gpu_seconds=? WHERE id=?",
                                        (now, now, gpu, self.estimate(), row['id']))
                        self.db.commit()
                if not row:
                    # Notifications handle our own jobs; bounded recheck notices an
                    # external Anchor process releasing its shared file lock.
                    self.changed.wait(0.5)
                    continue
            started = time.monotonic()
            try:
                state, result, error = self.executor(row['id'], row['request'], lock)
            except Exception as exc:
                state, result, error = 'failed', None, '{}: {}'.format(type(exc).__name__, exc)
            finally:
                os.close(lock)
            # A terminal receipt promises cleanup, including release of the GPU
            # lease. Publish it only after other users can acquire that lease.
            self.finish(row['id'], state, result, error, time.monotonic() - started)

    def execute(self, job_id, request, lock):
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        (directory / 'request.json').write_bytes(canonical(request))
        inventory = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid',
                                             '--format=csv,noheader'], text=True, timeout=20)
        with self.mutex:
            assigned_gpu = self.db.execute('SELECT assigned_gpu FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        if assigned_gpu is None:  # Direct executor invocation by acceptance tests.
            assigned_gpu = request['gpu']
        gpu_uuid = dict((r[0].strip(), r[1].strip()) for r in csv.reader(inventory.splitlines()))[str(assigned_gpu)]
        # Only a registered native workload runs here. No code upload or shell command API.
        allowed = {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH',
                   'VK_ICD_FILENAMES', 'XDG_RUNTIME_DIR', 'ISAACSIM_PATH', 'ISAAC_PATH',
                   'CARB_APP_PATH', 'EXP_PATH', 'ACCEPT_EULA', 'OMNI_KIT_ACCEPT_EULA',
                   'ANCHOR_GPU_RUNTIME_ROOT', 'ANCHOR_NVIDIA_DRIVER_DIR', 'ANCHOR_VULKAN_ENV'}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(CUDA_VISIBLE_DEVICES=gpu_uuid, NVIDIA_VISIBLE_DEVICES=gpu_uuid,
                   PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1', ZIWU_GPU_INDEX=str(assigned_gpu))
        command = [self.python, str(Path(__file__).with_name('isaac_drop.py')),
                   str(directory / 'request.json'), str(directory / 'result.json')]
        bimanual = request['workload'] == 'bimanual-session-v1'
        timeout = self.timeout
        if bimanual:
            command = [sys.executable, str(Path(__file__).with_name('bimanual_worker.py')),
                       '--deployment', str(task_deployment(request['parameters']['task_digest'])),
                       '--directory', str(directory), '--python', self.python,
                       '--online-limit', str(request['parameters']['online_limit'])]
            timeout = request['parameters']['online_limit'] + 900 + 7250 * 2 + 60
        started = time.monotonic()
        with (directory / 'runtime.log').open('w') as log:
            process = subprocess.Popen(command, cwd=directory, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(lock,))
            state = None
            try:
                while process.poll() is None:
                    if self.cancelled(job_id):
                        state = 'cancelled'
                        break
                    if time.monotonic() - started > timeout:
                        state = 'timed_out'
                        break
                    time.sleep(0.2)
            finally:
                kill_group(process)
            if state:
                return state, None, 'Native process group stopped: ' + state
            if process.returncode != 0:
                return 'failed', None, 'Isaac exited with code {}; inspect runtime.log'.format(process.returncode)
        if bimanual:
            state = json.loads((directory/'session-state.json').read_text())
            if state.get('state') != 'complete':
                raise ValueError('Bimanual replay not complete')
            result = dict(schema=SCHEMA, request_sha256=digest(request), status='passed',
                          reward=state['reward'], metrics=state['metrics'],
                          network_isolation=state['network_isolation'])
        else:
            result = json.loads((directory / 'result.json').read_text())
        result = validate_result(result, request)
        result['duration_sec'] = round(time.monotonic() - started, 3)
        result['exit_code'] = process.returncode
        result['implementation_version'] = Path(__file__).resolve().parents[1].name
        return 'succeeded', result, None


def handler(queue, token, deployer=None):
    owner = digest({'token': token})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log authorization headers or payloads.

        protocol_version = 'HTTP/1.1'

        def reply(self, code, data, *, raw=False, native_seconds=None):
            body = data if raw else canonical(data)
            compressed = raw and self.headers.get('Accept-Encoding') == 'gzip'
            if compressed:
                body = gzip.compress(body, compresslevel=1, mtime=0)
            # Keep only successful bounded task exchanges alive. Administrative
            # errors and rejected/unread request bodies always close the socket.
            keep = raw and self.headers.get('Connection', '').lower() == 'keep-alive'
            self.close_connection = not keep
            if keep:
                # The kernel may buffer the complete image before the remote
                # CPU has received it. Its next request cannot arrive until
                # that transfer finishes, so the ten-second *request body*
                # bound must not double as an authenticated idle deadline.
                self.connection.settimeout(300)
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'keep-alive' if keep else 'close')
            if compressed:
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Vary', 'Accept-Encoding')
            if native_seconds is not None:
                self.send_header('Server-Timing', f'native;dur={native_seconds*1000:.3f}')
            if code == 429:
                self.send_header('Retry-After', '2')
            self.end_headers()
            self.wfile.write(body)

        def handle_request(self):
            self.connection.settimeout(10)
            # Public task capability: only the existing numeric /call and
            # /health interfaces. It cannot submit jobs, finish, read recordings
            # or obtain another session's endpoint. Native service checks token.
            public = re.fullmatch(r'/sessions/([0-9a-f]{32})/(call|health)', self.path)
            if public and ((self.command == 'POST' and public[2] == 'call') or
                           (self.command == 'GET' and public[2] == 'health')):
                return self.public_call(public[1], public[2])
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                return self.reply(401, {'error': 'Unauthorized'})
            try:
                if self.command == 'GET' and self.path == '/health':
                    return self.reply(200, queue.health())
                if deployer and self.command == 'POST' and self.path == '/deployments':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= Deployer.LIMIT:
                        raise ValueError('Invalid task bundle size')
                    self.connection.settimeout(120)
                    bundle = self.rfile.read(size)
                    if len(bundle) != size:
                        raise ValueError('Incomplete task bundle')
                    return self.reply(202, deployer.submit(self.headers.get('X-Task-Digest', ''), bundle))
                installed = re.fullmatch(r'/deployments/([0-9a-f]{64})', self.path)
                if deployer and installed and self.command == 'GET':
                    return self.reply(200, deployer.status(installed[1]))
                if self.command == 'POST' and self.path == '/jobs':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192:
                        raise ValueError('Invalid body size')
                    data = json.loads(self.rfile.read(size))
                    return self.reply(202, queue.submit(owner, self.headers.get('Idempotency-Key', ''), data))
                log = re.fullmatch(r'/jobs/([0-9a-f]{32})/log(?:\?offset=([0-9]{1,12}))?', self.path)
                if log and self.command == 'GET':
                    return self.reply(200, queue.log(owner, log[1], int(log[2] or '0')))
                match = re.fullmatch(r'/jobs/([0-9a-f]{32})(/cancel)?', self.path)
                if match and self.command == 'GET' and not match[2]:
                    return self.reply(200, queue.get(owner, match[1]))
                if match and self.command == 'POST' and match[2]:
                    return self.reply(200, queue.cancel(owner, match[1]))
                session = re.fullmatch(r'/jobs/([0-9a-f]{32})/session(?:/(finish))?', self.path)
                if session:
                    job = queue.get(owner, session[1])
                    if job['request']['workload'] != 'bimanual-session-v1':
                        raise ValueError('Not a bimanual session')
                    directory = queue.root/session[1]
                    if session[2] and self.command == 'POST':
                        if job['state'] != 'running':
                            raise ValueError('Session is not running')
                        marker = directory/'finish-command.json'
                        if not marker.exists():
                            marker.write_text('{"finish": true}\n')
                        return self.reply(200, {'finish_requested': True})
                    if not session[2] and self.command == 'GET':
                        state_file = directory/'session-state.json'
                        state = json.loads(state_file.read_text()) if state_file.exists() else {'state':job['state']}
                        endpoint = directory/'online/session/endpoint.json'
                        if state['state'] == 'ready' and endpoint.exists():
                            native = json.loads(endpoint.read_text())
                            state['endpoint'] = {'path': '/sessions/'+session[1], 'token': native['token']}
                        return self.reply(200, state)
                return self.reply(404, {'error': 'Unknown route'})
            except QueueFull as exc:
                return self.reply(429, {'error': str(exc)})
            except KeyError:
                return self.reply(404, {'error': 'Unknown job'})
            except (ValueError, TypeError) as exc:
                return self.reply(400, {'error': str(exc)})

        def public_call(self, job_id, action):
            path = queue.root/job_id/'online/session/endpoint.json'
            if not path.exists():
                return self.reply(404, {'error':'Unknown session'})
            endpoint = json.loads(path.read_text())
            authorization = self.headers.get('Authorization', '')
            if not hmac.compare_digest(authorization, 'Bearer '+endpoint['token']):
                return self.reply(401, {'error':'Session capability required'})
            # Close admission at the trusted finish boundary, before replay.
            if (queue.root/job_id/'finish-command.json').exists():
                return self.reply(409, {'error':'Session is closed for collection'})
            body = None
            if self.command == 'POST':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536:
                        raise ValueError('Invalid request size')
                    body = self.rfile.read(size)
                    if len(body) != size: raise ValueError('Incomplete request')
                except (ValueError, OSError):
                    return self.reply(400, {'error':'Invalid bounded task request'})
            request = urllib.request.Request(endpoint['url']+'/'+action, data=body,
                        headers={'Authorization':authorization, 'Content-Type':'application/json'}, method=self.command)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            started = time.monotonic()
            try:
                with opener.open(request, timeout=240) as response:
                    raw = response.read(64*1024**2+1)
                    if len(raw)>64*1024**2: raise ValueError('Task response too large')
                    return self.reply(response.status, raw, raw=True,
                                      native_seconds=time.monotonic()-started)
            except urllib.error.HTTPError as exc:
                return self.reply(exc.code, {'error':'Task request rejected'})
            except (OSError, ValueError, urllib.error.URLError):
                return self.reply(503, {'error':'Task outcome unknown; retrieve same request receipt'})

        do_GET = handle_request
        do_POST = handle_request
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--lock-dir', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--gpus', default='0')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    if len(token) < 32 or args.token_file.stat().st_mode & 0o077:
        raise ValueError('Token file must have mode 600 and a token of at least 32 characters')
    gpus = [int(gpu) for gpu in args.gpus.split(',')]
    if not gpus or len(set(gpus)) != len(gpus) or any(gpu not in range(4) for gpu in gpus):
        raise ValueError('Invalid GPUs')
    queue = Queue(args.root, args.lock_dir, args.python, gpus, args.timeout)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(queue, token, Deployer(args.root, args.python)))
    server.daemon_threads = True
    def stop(*_):
        queue.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    queue.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        queue.close()


if __name__ == '__main__':
    main()
