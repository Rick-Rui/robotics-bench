"""Harbor CPU Docker adapter for the registered remote bimanual simulator."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'anchor-gpu/remote'))
from client import Client as TransportClient
from protocol import TERMINAL, validate_result
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lanes import parse_gpu

COLLECT = '/usr/local/bin/collect-remote-recording'


from transport import relay


class RemoteIsaacEnvironment(DockerEnvironment):
    def __init__(self, *args, **kwargs):
        self._remote_url = kwargs.pop('remote_url')
        self._private = Path(kwargs.pop('remote_private'))
        self._gpu = parse_gpu(kwargs.pop('remote_gpu'))   # 'auto' or a lane
        self._request_id = kwargs.pop('remote_request_id')
        self._task_digest = kwargs.pop('remote_task_digest')
        self._control = TransportClient(self._remote_url, Path(kwargs.pop('remote_token_file')).read_text(),
                                        request_timeout=120)
        self._relay = None
        self._job_id = None
        super().__init__(*args, **kwargs)
        role_lines = [line.strip() for line in (self.environment_dir/'remote-role.txt').read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith('#')]
        self._remote_role = role_lines[0] if len(role_lines) == 1 else ''
        if self._remote_role not in ('agent','verifier'):
            raise ValueError('Unexpected remote environment role')

    def _start_session(self):
        request = dict(workload='bimanual-session-v1', gpu=self._gpu,
                       parameters={'online_limit':6600,'task_digest':self._task_digest},
                       scheduling={'trial':self._request_id,'phase':'final'})
        job = self._control.submit(request, self._request_id)
        self._job_id = job['job_id']
        record = json.dumps({'job_id':self._job_id,'request':request})
        (self._private/'session.json').write_text(record)
        evidence = self.trial_paths.trial_dir/'remote-evidence'
        evidence.mkdir(exist_ok=True)
        (evidence/'session.json').write_text(record)
        deadline = time.monotonic()+1500
        while time.monotonic()<deadline:
            job = self._control.status(self._job_id)
            if job['state'] in TERMINAL:
                raise RuntimeError('Session could not start: '+str(job['error']))
            state = self._control.request('/jobs/'+self._job_id+'/session')
            if state['state']=='ready':
                # The card the host assigned (the request may have asked for 'auto').
                record = json.dumps({'job_id':self._job_id,'request':request,'assigned_gpu':job.get('assigned_gpu')})
                (self._private/'session.json').write_text(record)
                (evidence/'session.json').write_text(record)
                return state['endpoint']
            if state['state']=='failed': raise RuntimeError(state['error'])
            time.sleep(2)
        raise TimeoutError('Session startup deadline')

    async def start(self, force_build):
        await super().start(force_build)
        if self._remote_role == 'verifier':
            # The separate no-network CPU verifier receives only the trusted
            # remote verdict. It never reads an agent-supplied reward or model key.
            receipt = self._private/'verified.json'
            if not receipt.is_file():
                raise RuntimeError('Trusted remote collection/replay did not finish')
            await self.upload_file(receipt, '/remote-verdict.json')
            return
        try:
            endpoint = await asyncio.to_thread(self._start_session)
            bridge = json.loads(subprocess.check_output(['docker','network','inspect','bridge'],text=True))
            bind = bridge[0]['IPAM']['Config'][0]['Gateway']
            self._relay = relay(self._remote_url+endpoint['path'], endpoint['token'], bind,
                                self.trial_paths.trial_dir/'remote-evidence/transport.jsonl')
            public = self._private/'sim-endpoint.json'
            public.write_text(json.dumps({'url':f'http://{bind}:{self._relay.server_port}',
                                          'token':endpoint['token']}))
            await self.upload_file(public, '/app/sim-endpoint.json')
            await self.upload_file(public, '/etc/ziwu-pen-endpoint.json')
            result = await super().exec('chmod 644 /app/sim-endpoint.json && '
                                        'chmod 600 /etc/ziwu-pen-endpoint.json', user='root')
            if result.return_code: raise RuntimeError('Cannot publish simulator endpoint')
            result = await super().exec('sudo -n /usr/local/bin/start-pen-render', user='agent')
            if result.return_code: raise RuntimeError('Simulator readiness command failed')
        except BaseException as exc:
            if self._job_id:
                try:
                    await asyncio.to_thread(self._control.cancel,self._job_id)
                except Exception as cleanup_error:
                    exc.add_note('Startup cancellation failed: '+type(cleanup_error).__name__)
            raise

    def _collect(self):
        if self._job_id is None: raise RuntimeError('Missing remote session identity')
        self._control.request('/jobs/'+self._job_id+'/session/finish', {})
        job = self._control.wait(self._job_id, timeout=14600, poll=3)
        validate_result(job['result'],job['request'])
        (self._private/'verified.json').write_text(json.dumps(job,allow_nan=False))
        evidence = self.trial_paths.trial_dir/'remote-evidence'
        evidence.mkdir(exist_ok=True)
        (evidence/'verified.json').write_text(json.dumps(job,allow_nan=False))

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if command == COLLECT:
            if self._remote_role!='agent' or user!='root':
                raise PermissionError('Only the trusted Harbor collection hook may finalize a session')
            await asyncio.to_thread(self._collect)
            return ExecResult(return_code=0, stdout='Remote recording sealed and replay verified.',stderr='')
        return await super().exec(command,cwd=cwd,env=env,timeout_sec=timeout_sec,user=user)

    async def stop(self, delete):
        try:
            if self._relay:
                await asyncio.to_thread(self._relay.shutdown)
                self._relay.server_close()
            if self._job_id:
                job = await asyncio.to_thread(self._control.status,self._job_id)
                if job['state'] not in TERMINAL:
                    await asyncio.to_thread(self._control.cancel,self._job_id)
                    deadline = time.monotonic()+45
                    while job['state'] not in TERMINAL and time.monotonic()<deadline:
                        await asyncio.sleep(1)
                        job = await asyncio.to_thread(self._control.status,self._job_id)
                evidence = self.trial_paths.trial_dir/'remote-evidence'
                evidence.mkdir(exist_ok=True)
                (evidence/'cleanup.json').write_text(json.dumps({'job_id':self._job_id,
                    'state':job['state'],'cleanup_confirmed':job['state'] in TERMINAL}))
                if job['state'] not in TERMINAL: raise RuntimeError('Remote cleanup not confirmed')
        finally:
            await super().stop(delete)
