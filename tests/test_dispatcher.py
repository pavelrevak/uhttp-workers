"""Tests for Dispatcher routing and request handling logic."""

import os
import time
import unittest
import tempfile
import multiprocessing as mp

from uhttp.workers import (
    Dispatcher, Worker, WorkerPool, Request, Response,
    api, sync, RejectRequest,
    MSG_RESPONSE, MSG_HEARTBEAT,
    _PendingRequest,
)


class DummyWorker(Worker):
    @api('/test', 'GET')
    def test_handler(self, request):
        return {'ok': True}


class MockClient:
    """Mock HttpConnection for testing dispatcher logic."""

    def __init__(self, method='GET', path='/', query=None, data=None,
            headers=None, content_type=None):
        self.method = method
        self.path = path
        self.query = query
        self.data = data
        self.headers = headers or {}
        self.content_type = content_type
        self.responded = False
        self.response_data = None
        self.response_status = None
        self.response_headers = None
        self.redirect_url = None
        self.file_path = None

    def respond(self, data=None, status=200, headers=None, cookies=None):
        self.responded = True
        self.response_data = data
        self.response_status = status
        self.response_headers = headers

    def respond_redirect(self, url, cookies=None):
        self.responded = True
        self.redirect_url = url

    def respond_file(self, path, headers=None):
        self.responded = True
        self.file_path = path


class TestDispatcherSyncRoutes(unittest.TestCase):

    def test_sync_handler_called(self):

        class TestDispatcher(Dispatcher):
            @sync('/health')
            def health(self, client, path_params):
                client.respond({'status': 'ok'})

        d = TestDispatcher.__new__(TestDispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = []
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._build_sync_routes()

        client = MockClient('GET', '/health')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_data, {'status': 'ok'})

    def test_sync_with_params(self):

        class TestDispatcher(Dispatcher):
            @sync('/item/{id:int}')
            def item(self, client, path_params):
                client.respond({'id': path_params['id']})

        d = TestDispatcher.__new__(TestDispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = []
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._build_sync_routes()

        client = MockClient('GET', '/item/42')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_data, {'id': 42})

    def test_sync_method_filter(self):

        class TestDispatcher(Dispatcher):
            @sync('/webhook', 'POST')
            def webhook(self, client, path_params):
                client.respond({'received': True})

        d = TestDispatcher.__new__(TestDispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = []
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._build_sync_routes()

        # GET should not match sync → falls through to pool (none) → 404
        client = MockClient('GET', '/webhook')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 404)

        # POST should match sync
        client = MockClient('POST', '/webhook')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_data, {'received': True})


class TestDispatcherDoCheck(unittest.TestCase):

    def test_reject_request(self):

        class AuthDispatcher(Dispatcher):
            def do_check(self, client):
                if 'x-api-key' not in client.headers:
                    client.respond({'error': 'unauthorized'}, status=401)
                    raise RejectRequest()

        pool = WorkerPool(DummyWorker, routes=['/api/**'])
        d = AuthDispatcher.__new__(AuthDispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._build_sync_routes()

        client = MockClient('GET', '/api/test')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 401)

    def test_pass_check(self):
        pool = WorkerPool(DummyWorker, routes=['/api/**'])
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0

        client = MockClient('GET', '/api/test')
        d._http_request(client)
        # should be dispatched (not responded directly)
        self.assertFalse(client.responded)
        self.assertIn(0, d._pending)


class TestDispatcherStaticFiles(unittest.TestCase):

    def _make_dispatcher(self, static_routes=None, pools=None):
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        if static_routes:
            for prefix, path in static_routes.items():
                d._static_routes[prefix] = os.path.abspath(path)
        d._pools = pools or []
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        return d

    def test_serve_static(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, 'test.txt')
            with open(filepath, 'w') as f:
                f.write('hello')

            d = self._make_dispatcher({'/static/': tmpdir})
            client = MockClient('GET', '/static/test.txt')
            d._http_request(client)
            self.assertTrue(client.responded)
            self.assertEqual(client.file_path, filepath)

    def test_static_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make_dispatcher({'/static/': tmpdir})
            client = MockClient('GET', '/static/nonexistent.txt')
            d._http_request(client)
            # no static file, no sync, no pool → not responded (falls through to 404)
            self.assertTrue(client.responded)
            self.assertEqual(client.response_status, 404)

    def test_path_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make_dispatcher({'/static/': tmpdir})
            client = MockClient('GET', '/static/../../../etc/passwd')
            d._http_request(client)
            # traversal blocked, falls through to 404
            self.assertTrue(client.responded)
            self.assertEqual(client.response_status, 404)


class TestDispatcherPoolRouting(unittest.TestCase):

    def setUp(self):
        self.pool_a = WorkerPool(
            DummyWorker, routes=['/api/a/**'])
        self.pool_b = WorkerPool(
            DummyWorker, routes=['/api/b/**'])
        self.pool_default = WorkerPool(DummyWorker, routes=None)

    def test_find_pool_specific(self):
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [self.pool_a, self.pool_b, self.pool_default]
        self.assertIs(d._find_pool('/api/a/test'), self.pool_a)
        self.assertIs(d._find_pool('/api/b/test'), self.pool_b)

    def test_find_pool_fallback(self):
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [self.pool_a, self.pool_b, self.pool_default]
        self.assertIs(d._find_pool('/other'), self.pool_default)

    def test_find_pool_no_match(self):
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [self.pool_a, self.pool_b]  # no fallback
        self.assertIsNone(d._find_pool('/other'))

    def test_dispatch_no_pool_returns_404(self):
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = []
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0

        client = MockClient('GET', '/anything')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 404)

    def test_dispatch_max_pending(self):
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [self.pool_default]
        d._pending = {i: None for i in range(10)}
        d._max_pending = 10
        d._next_request_id = 100

        client = MockClient('GET', '/test')
        d._dispatch_to_pool(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 503)

    def test_dispatch_degraded_pool(self):
        self.pool_default._degraded = True
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [self.pool_default]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0

        client = MockClient('GET', '/test')
        d._dispatch_to_pool(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 503)


class TestDispatcherProcessResponse(unittest.TestCase):

    def test_process_response(self):
        pool = WorkerPool(DummyWorker, routes=None, timeout=30)
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [pool]
        d._pending = {}
        d._log_is_tty = False

        client = MockClient()
        d._pending[1] = _PendingRequest(client, pool)

        resp = Response(1, data={'result': 'ok'}, status=200)
        d._process_response((MSG_RESPONSE, 1, resp))
        self.assertTrue(client.responded)
        self.assertEqual(client.response_data, {'result': 'ok'})
        self.assertNotIn(1, d._pending)

    def test_process_response_unknown_id(self):
        d = Dispatcher.__new__(Dispatcher)
        d._pools = []
        d._pending = {}
        # should not raise
        resp = Response(999, data={'result': 'ok'})
        d._process_response((MSG_RESPONSE, 999, resp))

    def test_process_heartbeat(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        pool._last_seen = {0: 0, 1: 0}
        pool._current_request = {0: None, 1: None}
        pool.workers = [None, None]  # placeholders
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [pool]
        d._pending = {}
        d._log_is_tty = False

        d._process_response((MSG_HEARTBEAT, 'DummyWorker', 0, 42))
        self.assertGreater(pool._last_seen[0], 0)
        self.assertEqual(pool._current_request[0], 42)


class TestDispatcherExpirePending(unittest.TestCase):

    def test_expire_timed_out(self):
        pool = WorkerPool(DummyWorker, routes=None, timeout=0.1)
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [pool]
        d._pending = {}
        d._log_is_tty = False
        d.on_log = lambda *_: None

        client = MockClient()
        pending = _PendingRequest(client, pool)
        pending.timestamp = time.time() - 1  # expired
        d._pending[1] = pending

        d._expire_pending()
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 504)
        self.assertNotIn(1, d._pending)

    def test_keep_valid(self):
        pool = WorkerPool(DummyWorker, routes=None, timeout=60)
        d = Dispatcher.__new__(Dispatcher)
        d._pools = [pool]
        d._pending = {}
        d._log_is_tty = False

        client = MockClient()
        d._pending[1] = _PendingRequest(client, pool)

        d._expire_pending()
        self.assertFalse(client.responded)
        self.assertIn(1, d._pending)


class TestDispatcherRoutePriority(unittest.TestCase):
    """Test that static > sync > pool routing order is respected."""

    def test_static_before_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, 'health.txt')
            with open(filepath, 'w') as f:
                f.write('static')

            class TestDispatcher(Dispatcher):
                @sync('/static/health.txt')
                def health(self, client, path_params):
                    client.respond({'sync': True})

            d = TestDispatcher.__new__(TestDispatcher)
            d._sync_routes = []
            d._static_routes = {'/static/': os.path.abspath(tmpdir)}
            d._pools = []
            d._pending = {}
            d._max_pending = 1000
            d._next_request_id = 0
            d._build_sync_routes()

            # static file exists → should serve static, not sync
            client = MockClient('GET', '/static/health.txt')
            d._http_request(client)
            self.assertTrue(client.responded)
            self.assertEqual(client.file_path, filepath)

    def test_sync_before_pool(self):

        class TestDispatcher(Dispatcher):
            @sync('/status')
            def status(self, client, path_params):
                client.respond({'sync': True})

        pool = WorkerPool(DummyWorker, routes=None)  # catches everything
        d = TestDispatcher.__new__(TestDispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._build_sync_routes()

        client = MockClient('GET', '/status')
        d._http_request(client)
        self.assertTrue(client.responded)
        self.assertEqual(client.response_data, {'sync': True})
        # should NOT be in pending (not dispatched to pool)
        self.assertEqual(len(d._pending), 0)


if __name__ == '__main__':
    unittest.main()
