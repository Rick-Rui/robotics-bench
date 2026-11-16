from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'anchor-gpu/remote'))
import server
sys.path.insert(0, str(HERE))
from transport import Connection, relay


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = server.Queue(Path(self.temp.name)/'jobs', Path(self.temp.name)/'locks', sys.executable, [1])
        self.calls = []
        self.payload = json.dumps({'observation': {'images': {'camera': 'abcdef'*10000}}, 'step': 17}).encode()
        case = self

        class Native(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                case.calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Length', str(len(case.payload)))
                self.end_headers()
                self.wfile.write(case.payload)

        self.native = ThreadingHTTPServer(('127.0.0.1', 0), Native)
        jid = 'a'*32
        endpoint = self.queue.root/jid/'online/session'
        endpoint.mkdir(parents=True)
        (endpoint/'endpoint.json').write_text(json.dumps({
            'url': f'http://127.0.0.1:{self.native.server_port}', 'token': 'session-token'}))
        self.accepts = []

        class CountingServer(ThreadingHTTPServer):
            def get_request(inner):
                sock, address = super().get_request()
                case.accepts.append(address)
                return sock, address

        self.proxy = CountingServer(('127.0.0.1', 0), server.handler(self.queue, 'admin-token'))
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.native, self.proxy)]
        for t in self.threads: t.start()
        self.client = Connection(f'http://127.0.0.1:{self.proxy.server_port}/sessions/{jid}', 'session-token')

    def tearDown(self):
        self.client.close()
        for s in (self.proxy, self.native):
            s.shutdown(); s.server_close()
        for t in self.threads: t.join()
        self.queue.close()
        self.temp.cleanup()

    def test_lossless_gzip_and_connection_reuse(self):
        for body in (b'{"request_id":"one"}', b'{"request_id":"two"}'):
            status, result, timing = self.client.request('POST', '/call', body)
            self.assertEqual(status, 200)
            self.assertEqual(result, self.payload)
            self.assertLess(timing['wire_bytes'], len(result)//10)
            self.assertTrue(timing['native_timing'].startswith('native;dur='))
        self.assertEqual(len(self.accepts), 1)
        self.assertEqual(len(self.calls), 2)

    def test_long_model_pause_opens_fresh_connection(self):
        self.client.request('POST', '/call', b'{}')
        self.client.last_used = 0
        self.client.request('POST', '/call', b'{}')
        self.assertEqual(len(self.accepts), 2)

    def test_decompressed_limit_and_no_implicit_retry(self):
        with patch('transport.LIMIT', 1024):
            with self.assertRaisesRegex(ValueError, 'Oversize decoded'):
                self.client.request('POST', '/call', b'{}')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.client.connection)

    def test_capability_still_required(self):
        self.client.capability = 'wrong-token'
        status, _, _ = self.client.request('POST', '/call', b'{}')
        self.assertEqual(status, 401)
        self.assertEqual(self.calls, [])

    def test_relay_returns_unknown_outcome_without_replaying_failed_request(self):
        # Exercise the actual nested HTTP handler, including its exception
        # clause: a listener variable must not shadow the http module.
        listener = relay(f'http://127.0.0.1:{self.proxy.server_port}/sessions/'+('a'*32),
                         'session-token', '127.0.0.1')
        try:
            with patch('transport.Connection.request', side_effect=ConnectionResetError('lost')) as call:
                req = urllib.request.Request(f'http://127.0.0.1:{listener.server_port}/call',
                    data=b'{"request_id":"same-id"}', headers={'Authorization':'Bearer session-token'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 503)
                self.assertIn(b'same request receipt', error.exception.read())
                self.assertEqual(call.call_count, 1)
        finally:
            listener.shutdown(); listener.server_close()

    def test_idle_deadline_allows_in_flight_image_transfer(self):
        # A successful authenticated response uses the transfer-sized idle
        # bound; the next request body is still bounded separately.
        timeouts = []
        original = server.handler
        def tracking_handler(queue, token):
            base = original(queue, token)
            class Tracking(base):
                def reply(inner, *args, **kwargs):
                    super().reply(*args, **kwargs)
                    timeouts.append(inner.connection.gettimeout())
            return Tracking
        self.proxy.RequestHandlerClass = tracking_handler(self.queue, 'admin-token')
        self.client.request('POST', '/call', b'{}')
        self.assertIn(300, timeouts)


if __name__ == '__main__':
    unittest.main()
