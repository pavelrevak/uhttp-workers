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
    MSG_SSE_OPEN, MSG_SSE_EVENT, MSG_SSE_CLOSE, MSG_NDJSON,
    CTL_DISCONNECT,
    PENDING_COMPLETED, PENDING_TIMEOUT, PENDING_DISCONNECTED,
    PENDING_STREAM_CLOSED, PENDING_SHUTDOWN, PENDING_WORKER_DIED,
    LOG_ERROR,
    _PendingRequest,
)


class DummyWorker(Worker):
    @api('/test', 'GET')
    def test_handler(self, request):
        return {'ok': True}


class MockClient:
    """Mock HttpConnection for testing dispatcher logic."""

    def __init__(self, method='GET', path='/', query=None, data=None,
            headers=None, content_type=None, body=None,
            remote_address='127.0.0.1:0'):
        self.method = method
        self.path = path
        self.query = query
        self.data = data
        self.headers = headers or {}
        self.content_type = content_type
        self.body = body
        self.remote_address = remote_address
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

    def response_stream(self, content_type=None, headers=None, cookies=None):
        self.streaming = True
        self.stream_content_type = content_type
        self.stream_headers = headers
        self.stream_cookies = cookies
        return True

    def send_event(self, data=None, event=None, event_id=None, retry=None):
        if not hasattr(self, '_events'):
            self._events = []
        self._events.append({
            'data': data, 'event': event,
            'event_id': event_id, 'retry': retry})
        return getattr(self, '_connected', True)

    def send_chunk(self, data):
        if not hasattr(self, '_chunks'):
            self._chunks = []
        self._chunks.append(data)
        return getattr(self, '_connected', True)

    def send_ndjson(self, obj):
        if not hasattr(self, '_ndjson'):
            self._ndjson = []
        self._ndjson.append(obj)
        return getattr(self, '_connected', True)

    def response_stream_end(self):
        self.stream_ended = True


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
        # fake a live worker so alive_count > 0 without starting processes
        pool.workers = [type('W', (), {'is_alive': lambda self: True})()]
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

    def test_dispatch_no_alive_workers_returns_503(self):
        """Transient: pool has workers but none currently alive."""
        # pool has dead workers (not degraded yet)
        self.pool_default.workers = [
            type('W', (), {'is_alive': lambda self: False})()]
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
        self.assertEqual(
            client.response_data['error'], 'No workers available')
        self.assertEqual(
            client.response_headers.get('Retry-After'), '1')
        # request was NOT enqueued
        self.assertEqual(d._pending, {})

    def test_dispatch_empty_workers_returns_503(self):
        """Pool was never started (empty workers list)."""
        # self.pool_default.workers is [] from __init__
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
        self.assertEqual(
            client.response_data['error'], 'No workers available')

    def test_dispatch_forwards_remote_address(self):
        """Request enqueued to worker carries client.remote_address."""
        # fake an alive worker so dispatch reaches enqueue
        self.pool_default.workers = [
            type('W', (), {'is_alive': lambda self: True})()]
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [self.pool_default]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0

        client = MockClient(
            'GET', '/test', remote_address='198.51.100.7:33421')
        d._dispatch_to_pool(client)
        # request was enqueued
        req = self.pool_default.request_queue.get(timeout=1)
        self.assertEqual(req.remote_address, '198.51.100.7:33421')


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


class TestDispatcherSSE(unittest.TestCase):

    def _make_dispatcher(self):
        pool = WorkerPool(DummyWorker, routes=['/api/**'])
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._response_queue = mp.Queue()
        d._log_is_tty = False
        d.on_log = lambda *_: None
        return d, pool

    def test_sse_open(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.worker_id = 0
        d._pending[1] = pending
        d._process_response(
            (MSG_SSE_OPEN, 1, 'text/event-stream', None, None))
        self.assertTrue(client.streaming)
        self.assertEqual(client.stream_content_type, 'text/event-stream')
        self.assertTrue(pending.streaming)
        # still in pending
        self.assertIn(1, d._pending)

    def test_sse_send_event(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        d._pending[1] = pending
        d._process_response(
            (MSG_SSE_EVENT, 1, {'count': 5}, 'update', '3', None))
        self.assertEqual(len(client._events), 1)
        self.assertEqual(client._events[0]['data'], {'count': 5})
        self.assertEqual(client._events[0]['event'], 'update')
        self.assertEqual(client._events[0]['event_id'], '3')

    def test_sse_send_chunk(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/stream')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        d._pending[1] = pending
        # send_chunk: event/event_id/retry are all None
        d._process_response(
            (MSG_SSE_EVENT, 1, b'raw data', None, None, None))
        self.assertEqual(len(client._chunks), 1)
        self.assertEqual(client._chunks[0], b'raw data')

    def test_sse_close(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        d._pending[1] = pending
        d._process_response((MSG_SSE_CLOSE, 1))
        self.assertTrue(client.stream_ended)
        # removed from pending
        self.assertNotIn(1, d._pending)

    def test_sse_client_disconnect(self):
        d, pool = self._make_dispatcher()
        pool.start(d._response_queue)
        client = MockClient('GET', '/api/events')
        client._connected = False  # simulate disconnected client
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        pending.worker_id = 0
        d._pending[1] = pending
        d._process_response(
            (MSG_SSE_EVENT, 1, {'data': 'test'}, 'ping', None, None))
        # removed from pending
        self.assertNotIn(1, d._pending)
        # CTL_DISCONNECT sent to worker's control queue
        msg = pool._control_queues[0].get(timeout=1)
        self.assertEqual(msg, (CTL_DISCONNECT, 1))
        pool.shutdown(timeout=2)

    def test_streaming_excluded_from_timeout(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        pending.timestamp = 0  # very old
        d._pending[1] = pending
        d._expire_pending()
        # streaming request should NOT be expired
        self.assertIn(1, d._pending)

    def test_non_streaming_expires(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        pending.timestamp = 0  # very old
        d._pending[1] = pending
        d._expire_pending()
        # non-streaming should be expired
        self.assertNotIn(1, d._pending)

    def test_ndjson_send(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/stream')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        d._pending[1] = pending
        d._process_response(
            (MSG_NDJSON, 1, {'devices': [1, 2, 3]}))
        self.assertEqual(len(client._ndjson), 1)
        self.assertEqual(client._ndjson[0], {'devices': [1, 2, 3]})
        # still pending — stream open
        self.assertIn(1, d._pending)

    def test_ndjson_client_disconnect(self):
        d, pool = self._make_dispatcher()
        pool.start(d._response_queue)
        client = MockClient('GET', '/api/stream')
        client._connected = False
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        pending.worker_id = 0
        d._pending[1] = pending
        d._process_response((MSG_NDJSON, 1, {'x': 1}))
        # removed from pending
        self.assertNotIn(1, d._pending)
        # CTL_DISCONNECT sent to worker's control queue
        msg = pool._control_queues[0].get(timeout=1)
        self.assertEqual(msg, (CTL_DISCONNECT, 1))
        pool.shutdown(timeout=2)

    def test_ndjson_ignored_after_close(self):
        d, pool = self._make_dispatcher()
        # no pending request with id 99
        d._process_response((MSG_NDJSON, 99, {'x': 1}))
        # should not raise, just ignore

    def test_sse_event_ignored_after_close(self):
        d, pool = self._make_dispatcher()
        # no pending request with id 99
        d._process_response(
            (MSG_SSE_EVENT, 99, {'data': 'test'}, None, None, None))
        # should not raise, just ignore


class TestDispatcherPendingRemoved(unittest.TestCase):
    """Tests for the on_pending_removed lifecycle hook."""

    def _make_dispatcher(self, dispatcher_cls=Dispatcher):
        pool = WorkerPool(DummyWorker, routes=['/api/**'])
        d = dispatcher_cls.__new__(dispatcher_cls)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._response_queue = mp.Queue()
        d._log_is_tty = False
        d.log_calls = []
        d.on_log = lambda name, level, msg: d.log_calls.append(
            (name, level, msg))
        d.recorded = []
        return d, pool

    def test_completed_fires_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        d._pending[1] = pending
        response = Response(request_id=1, data={'ok': True}, status=200)
        d._process_response((MSG_RESPONSE, 1, response))
        self.assertEqual(d.recorded, [(1, PENDING_COMPLETED)])

    def test_completed_calls_on_response_before_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_response(self, response, pending):
                self.recorded.append(('on_response', response.request_id))

            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append(('hook', request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        d._pending[1] = pending
        response = Response(request_id=1, data={'ok': True}, status=200)
        d._process_response((MSG_RESPONSE, 1, response))
        self.assertEqual(d.recorded, [
            ('on_response', 1),
            ('hook', 1, PENDING_COMPLETED),
        ])

    def test_timeout_fires_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        pending.timestamp = 0  # very old
        d._pending[1] = pending
        d._expire_pending()
        self.assertNotIn(1, d._pending)
        self.assertEqual(client.response_status, 504)
        self.assertEqual(d.recorded, [(1, PENDING_TIMEOUT)])

    def test_stream_closed_fires_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        d._pending[1] = pending
        d._process_response((MSG_SSE_CLOSE, 1))
        self.assertTrue(client.stream_ended)
        self.assertEqual(d.recorded, [(1, PENDING_STREAM_CLOSED)])

    def test_disconnect_fires_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient('GET', '/api/events')
        client._connected = False
        pending = _PendingRequest(client, pool)
        pending.streaming = True
        pending.worker_id = None  # skip control queue routing
        d._pending[1] = pending
        d._process_response(
            (MSG_SSE_EVENT, 1, {'data': 'x'}, 'ping', None, None))
        self.assertNotIn(1, d._pending)
        self.assertEqual(d.recorded, [(1, PENDING_DISCONNECTED)])

    def test_shutdown_fires_hook(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)

        class FakeHttpServer:
            def close(self):
                pass

        class FakePool:
            def shutdown(self, timeout):
                pass

        d._http_server = FakeHttpServer()
        d._pools = [FakePool()]
        d._shutdown_timeout = 0.0  # skip drain loop immediately

        client_a = MockClient('GET', '/api/a')
        client_b = MockClient('GET', '/api/b')
        d._pending[1] = _PendingRequest(client_a, pool)
        d._pending[2] = _PendingRequest(client_b, pool)

        d._shutdown()

        self.assertEqual(client_a.response_status, 503)
        self.assertEqual(client_b.response_status, 503)
        self.assertEqual(sorted(d.recorded), [
            (1, PENDING_SHUTDOWN),
            (2, PENDING_SHUTDOWN),
        ])
        self.assertEqual(d._pending, {})

    def test_hook_exception_is_swallowed_and_logged(self):

        class BrokenDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                raise RuntimeError('boom')

        d, pool = self._make_dispatcher(BrokenDispatcher)
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        d._pending[1] = pending
        response = Response(request_id=1, data={'ok': True}, status=200)
        # must not propagate
        d._process_response((MSG_RESPONSE, 1, response))
        # client still got the response
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 200)
        # error was logged
        error_logs = [
            msg for _, level, msg in d.log_calls if level == LOG_ERROR]
        self.assertEqual(len(error_logs), 1)
        self.assertIn('on_pending_removed', error_logs[0])
        self.assertIn('boom', error_logs[0])

    def test_default_hook_is_noop(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        d._pending[1] = pending
        response = Response(request_id=1, data={'ok': True}, status=200)
        # base Dispatcher has no-op on_pending_removed → must not raise
        d._process_response((MSG_RESPONSE, 1, response))
        self.assertTrue(client.responded)


class TestDispatcherWorkerDied(unittest.TestCase):
    """Tests for on_worker_died hook and victim handling."""

    def _make_dispatcher(self, dispatcher_cls=Dispatcher):
        # queue_warning=0 disables the queue-size check (which would
        # otherwise touch pool.pending_count → request_queue.qsize()).
        pool = WorkerPool(
            DummyWorker, routes=['/api/**'], queue_warning=0)
        # Mock check_workers so we control what it returns without
        # actually starting processes.
        pool._fake_restarted = []
        pool.check_workers = lambda: pool._fake_restarted
        d = dispatcher_cls.__new__(dispatcher_cls)
        d._sync_routes = []
        d._static_routes = {}
        d._pools = [pool]
        d._pending = {}
        d._max_pending = 1000
        d._next_request_id = 0
        d._response_queue = mp.Queue()
        d._log_is_tty = False
        d.log_calls = []
        d.on_log = lambda name, level, msg: d.log_calls.append(
            (name, level, msg))
        d.recorded_removed = []
        return d, pool

    def test_single_victim_gets_500(self):

        class RecordingDispatcher(Dispatcher):
            def on_pending_removed(self, request_id, pending, reason):
                self.recorded_removed.append((request_id, reason))

        d, pool = self._make_dispatcher(RecordingDispatcher)
        client = MockClient(
            'POST', '/api/scan', body=b'\x00\x01bad',
            remote_address='10.0.0.7:42')
        pending = _PendingRequest(client, pool)
        pending.worker_id = 0
        d._pending[42] = pending
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        d._check_all_workers()
        # client got 500
        self.assertTrue(client.responded)
        self.assertEqual(client.response_status, 500)
        self.assertEqual(client.response_data['error'], 'Worker crashed')
        self.assertIn('exit=-11', client.response_data['reason'])
        # removed from pending + hook fired
        self.assertNotIn(42, d._pending)
        self.assertEqual(
            d.recorded_removed, [(42, PENDING_WORKER_DIED)])

    def test_multiple_victims_all_handled(self):
        d, pool = self._make_dispatcher()
        c1 = MockClient('GET', '/api/a', remote_address='1.1.1.1:42')
        c2 = MockClient('GET', '/api/b', remote_address='2.2.2.2:42')
        c3 = MockClient('GET', '/api/c', remote_address='3.3.3.3:42')
        for rid, c in [(1, c1), (2, c2), (3, c3)]:
            p = _PendingRequest(c, pool)
            p.worker_id = 0
            d._pending[rid] = p
        pool._fake_restarted = [(0, 'stuck', None)]
        d._check_all_workers()
        for c in (c1, c2, c3):
            self.assertTrue(c.responded)
            self.assertEqual(c.response_status, 500)
        self.assertEqual(d._pending, {})

    def test_streaming_victim_gets_stream_end(self):
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/events')
        pending = _PendingRequest(client, pool)
        pending.worker_id = 0
        pending.streaming = True
        d._pending[1] = pending
        pool._fake_restarted = [(0, 'died exit=-9', -9)]
        d._check_all_workers()
        # stream ended, NOT respond()
        self.assertTrue(getattr(client, 'stream_ended', False))
        self.assertFalse(client.responded)
        self.assertNotIn(1, d._pending)

    def test_queued_request_not_a_victim(self):
        """Request with worker_id=None is still in queue — not a victim."""
        d, pool = self._make_dispatcher()
        # request belonging to dying worker
        in_flight = MockClient('GET', '/api/active')
        p1 = _PendingRequest(in_flight, pool)
        p1.worker_id = 0
        d._pending[1] = p1
        # request still in queue, no worker claimed it
        queued = MockClient('GET', '/api/queued')
        p2 = _PendingRequest(queued, pool)
        # p2.worker_id stays None
        d._pending[2] = p2
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        d._check_all_workers()
        # in-flight responded
        self.assertTrue(in_flight.responded)
        self.assertNotIn(1, d._pending)
        # queued untouched
        self.assertFalse(queued.responded)
        self.assertIn(2, d._pending)

    def test_other_worker_not_affected(self):
        """Only victims of THIS worker are handled; other workers stay."""
        d, pool = self._make_dispatcher()
        c1 = MockClient('GET', '/api/a')
        c2 = MockClient('GET', '/api/b')
        p1 = _PendingRequest(c1, pool)
        p1.worker_id = 0
        p2 = _PendingRequest(c2, pool)
        p2.worker_id = 1
        d._pending[1] = p1
        d._pending[2] = p2
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        d._check_all_workers()
        self.assertTrue(c1.responded)
        self.assertNotIn(1, d._pending)
        self.assertFalse(c2.responded)
        self.assertIn(2, d._pending)

    def test_late_response_after_victim_cleanup_dropped(self):
        """MSG_RESPONSE from dead worker arriving after victim removal is dropped."""
        d, pool = self._make_dispatcher()
        client = MockClient('GET', '/api/test')
        pending = _PendingRequest(client, pool)
        pending.worker_id = 0
        d._pending[1] = pending
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        d._check_all_workers()
        # request already gone; client already got 500
        self.assertEqual(client.response_status, 500)
        # late response from before-death — must not break or double-respond
        client.response_status = None
        late = Response(request_id=1, data={'ok': True}, status=200)
        d._process_response((MSG_RESPONSE, 1, late))
        # silently dropped
        self.assertIsNone(client.response_status)

    def test_no_victims_just_logs(self):
        """Worker died while idle — restarted but no pending requests."""
        d, pool = self._make_dispatcher()
        pool._fake_restarted = [(0, 'died exit=0', 0)]
        d._check_all_workers()
        # no crash, no pending changes
        self.assertEqual(d._pending, {})
        # should have logged
        error_logs = [
            msg for _, level, msg in d.log_calls if level == LOG_ERROR]
        self.assertEqual(len(error_logs), 1)
        self.assertIn('victims=0', error_logs[0])

    def test_override_can_persist_payload(self):
        """User override can capture victim payload before super() responds."""
        captured = []

        class ForensicDispatcher(Dispatcher):
            def on_worker_died(
                    self, pool, worker_id, reason, exitcode, victims):
                for rid, pending in victims:
                    captured.append({
                        'rid': rid,
                        'remote_address': pending.client.remote_address,
                        'body': pending.client.body,
                        'reason': reason,
                        'exitcode': exitcode})
                super().on_worker_died(
                    pool, worker_id, reason, exitcode, victims)

        d, pool = self._make_dispatcher(ForensicDispatcher)
        client = MockClient(
            'POST', '/api/process',
            body=b'\xff\xfecorrupted', remote_address='9.9.9.9:42')
        pending = _PendingRequest(client, pool)
        pending.worker_id = 0
        d._pending[7] = pending
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        d._check_all_workers()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['remote_address'], '9.9.9.9:42')
        self.assertEqual(captured[0]['body'], b'\xff\xfecorrupted')
        self.assertEqual(captured[0]['exitcode'], -11)
        # super() still ran
        self.assertEqual(client.response_status, 500)

    def test_hook_exception_does_not_crash_dispatcher(self):

        class BrokenDispatcher(Dispatcher):
            def on_worker_died(self, *args, **kwargs):
                raise RuntimeError('boom')

        d, pool = self._make_dispatcher(BrokenDispatcher)
        pool._fake_restarted = [(0, 'died exit=-11', -11)]
        # must not propagate
        d._check_all_workers()
        error_logs = [
            msg for _, level, msg in d.log_calls if level == LOG_ERROR]
        # one error log about the hook failure
        self.assertTrue(any('on_worker_died' in m for m in error_logs))
        self.assertTrue(any('boom' in m for m in error_logs))


if __name__ == '__main__':
    unittest.main()
