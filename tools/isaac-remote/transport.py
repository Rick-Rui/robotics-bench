"""Lossless, bounded HTTP relay; simulator requests are never retried implicitly."""
import gzip
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

LIMIT = 64 * 1024**2


class Connection:
    def __init__(self, url, capability):
        self.url = urlsplit(url)
        if self.url.scheme != 'http' or self.url.hostname not in ('127.0.0.1', 'localhost'):
            raise ValueError('Remote transport requires a local SSH tunnel')
        self.capability = capability
        self.lock = threading.Lock()
        self.connection = None
        self.last_used = 0

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        if self.connection:
            self.connection.close()
        self.connection = None

    def request(self, method, path, data):
        with self.lock:
            # The remote HTTP server has a ten-second idle bound. Do not reuse
            # a connection across a model's longer thinking interval.
            if time.monotonic() - self.last_used > 5:
                self._close()
            if self.connection is None:
                self.connection = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=240)
            started = time.monotonic()
            try:
                self.connection.request(method, self.url.path+path, body=data, headers={
                    'Authorization': 'Bearer '+self.capability,
                    'Content-Type': 'application/json', 'Accept-Encoding': 'gzip',
                    'Connection': 'keep-alive'})
                response = self.connection.getresponse()
                headers_at = time.monotonic()
                wire = response.read(LIMIT+1)
                if len(wire) > LIMIT:
                    raise ValueError('Oversize wire response')
                encoding = response.getheader('Content-Encoding', 'identity')
                if encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(wire)) as compressed:
                        body = compressed.read(LIMIT+1)
                elif encoding == 'identity':
                    body = wire
                else:
                    raise ValueError('Unsupported response encoding')
                if len(body) > LIMIT:
                    raise ValueError('Oversize decoded response')
                receipt = json.loads(body)
                timing = dict(status=response.status, wire_bytes=len(wire), decoded_bytes=len(body),
                              headers_seconds=headers_at-started, total_seconds=time.monotonic()-started,
                              native_timing=response.getheader('Server-Timing'), end_step=receipt.get('end_step'))
                self.last_used = time.monotonic()
                return response.status, body, timing
            except BaseException:
                # A write may have reached the simulator. Only the caller can
                # retrieve the SAME request ID; never replay a mutation here.
                self._close()
                raise


def relay(remote_url, capability, bind, evidence=None):
    transport = Connection(remote_url, capability)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            self.connection.settimeout(10)
            if (self.path, self.command) not in (('/call', 'POST'), ('/health', 'GET')):
                self.send_error(404); return
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+capability):
                self.send_error(401); return
            data = None
            if self.command == 'POST':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536:
                        raise ValueError('Invalid request size')
                    data = self.rfile.read(size)
                    if len(data) != size:
                        raise ValueError('Incomplete request')
                except (ValueError, OSError):
                    self.send_error(400); return
            try:
                code, body, timing = transport.request(self.command, self.path, data)
                if evidence:
                    path = Path(evidence)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('a') as stream:
                        stream.write(json.dumps(timing)+'\n')
            except (OSError, ValueError, EOFError, http.client.HTTPException):
                code, body = 503, b'{"error":"Outcome unknown; retrieve the same request receipt"}'
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = handle_request
        do_POST = handle_request

    class Relay(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            super().server_close()
            transport.close()

    listener = Relay((bind, 0), Handler)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    return listener
