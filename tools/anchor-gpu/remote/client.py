"""CPU-only client: submit once, poll asynchronously, retrieve validated results."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, build_opener, ProxyHandler
import uuid
from urllib.error import URLError

from protocol import TERMINAL, canonical, digest, validate, validate_result


class TransportError(RuntimeError):
    """A control request failed; a mutation may already have been accepted."""


class Client:
    def __init__(self, url, token, request_timeout=20):
        if not url.startswith(('http://127.0.0.1:', 'http://localhost:', 'https://')):
            raise ValueError('Use a local SSH tunnel or HTTPS')
        self.url, self.token = url.rstrip('/'), token.strip()
        self.request_timeout = request_timeout
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data=None, key=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if key:
            headers['Idempotency-Key'] = key
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(self.url + path, data=canonical(data) if data is not None else None, headers=headers)
        try:
            with self.opener.open(req, timeout=self.request_timeout) as response:
                return json.load(response)
        except TimeoutError as exc:
            # Do not let Harbor mistake a socket timeout for its overall
            # environment-start deadline. Never resubmit a mutation here.
            raise TransportError(f'Control request timed out after {self.request_timeout}s; '
                                 'remote outcome must be checked') from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TransportError(f'Control request timed out after {self.request_timeout}s; '
                                     'remote outcome must be checked') from exc
            raise

    def submit(self, request, key):
        return self.request('/jobs', validate(request), key)

    def status(self, job_id):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('Invalid job id')
        return self.request('/jobs/' + job_id)

    def cancel(self, job_id):
        self.status(job_id)
        return self.request('/jobs/' + job_id + '/cancel', {})

    def log(self, job_id, offset=0):
        self.status(job_id)
        return self.request('/jobs/' + job_id + '/log?offset=' + str(offset))

    def wait(self, job_id, timeout=420, poll=2):
        deadline = time.monotonic() + timeout
        while True:
            job = self.status(job_id)
            if job['state'] in TERMINAL:
                if job['state'] != 'succeeded':
                    raise RuntimeError('{}: {}'.format(job['state'], job['error']))
                validate_result(job['result'], job['request'])
                return job
            if time.monotonic() >= deadline:
                self.cancel(job_id)
                raise TimeoutError('Remote wait timed out; cancellation requested')
            time.sleep(poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18765')
    parser.add_argument('--token-file', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('health')
    submit = commands.add_parser('submit')
    submit.add_argument('--request', type=Path, required=True)
    submit.add_argument('--key', default=None)
    for name in ['status', 'wait', 'cancel', 'log']:
        commands.add_parser(name).add_argument('job_id')
    args = parser.parse_args()
    client = Client(args.url, args.token_file.read_text())
    if args.command == 'health':
        result = client.request('/health')
    elif args.command == 'submit':
        key = args.key or uuid.uuid4().hex
        result = client.submit(json.loads(args.request.read_text()), key)
        result['idempotency_key'] = key
    else:
        result = getattr(client, args.command)(args.job_id)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
