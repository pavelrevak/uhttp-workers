"""End-to-end integration tests.

A single real dispatcher (event loop pumped in a daemon thread, no signals),
real worker pools (real processes), and real HTTP clients (uhttp-client) over
real sockets. The server is started ONCE for the whole module (setUpModule)
with a rich configuration; every test runs against that one instance.

Request/response and concurrency use uhttp-client (itself select-based, so many
requests are driven by one select loop). Open streams (SSE/NDJSON/chunked) are
read with a raw socket, since the client consumes a full body, not a live stream.
"""

import time
import select
import socket
import threading
import unittest

import uhttp.workers as w
from uhttp.client import (
    HttpClient,
    EVENT_HEADERS, EVENT_DATA, EVENT_COMPLETE, EVENT_ERROR)


# --- test workers -------------------------------------------------------

class EchoWorker(w.Worker):
    @w.api('/echo', 'GET', 'POST')
    def echo(self, request):
        return {
            'method': request.method,
            'data': request.data,
            'query': request.query,
            'server': request.server,
        }


class SlowWorker(w.Worker):
    """Blocking handler — parallelism comes from multiple workers."""

    @w.api('/slow', 'GET')
    def slow(self, request):
        time.sleep(0.3)
        return {'slept': 0.3, 'worker': self.worker_id}


class DeferredWorker(w.Worker):
    """Non-blocking: returns DEFERRED, responds later from on_idle. A single
    worker can have many requests in flight at once."""

    def setup(self):
        self._pending = []

    @w.api('/deferred', 'GET')
    def deferred(self, request):
        self._pending.append((time.time() + 0.3, request))
        return w.DEFERRED

    def on_idle(self):
        now = time.time()
        keep = []
        for deadline, request in self._pending:
            if now >= deadline:
                request.respond({'deferred': True, 'worker': self.worker_id})
            else:
                keep.append((deadline, request))
        self._pending = keep


class MatrixWorker(w.Worker):
    @w.api('/pub', 'GET')
    def pub(self, request):
        return {'server': request.server, 'pool': self.pool_name}

    @w.api('/int', 'GET')
    def internal(self, request):
        return {'server': request.server, 'pool': self.pool_name}


class StreamWorker(w.Worker):
    """SSE, NDJSON, and raw chunked streaming (response side)."""

    @w.api('/sse', 'GET')
    def sse(self, request):
        request.response_stream(content_type='text/event-stream')
        for i in range(3):
            request.send_event(data={'n': i}, event='tick')
        request.response_stream_end()
        return w.DEFERRED

    @w.api('/ndjson', 'GET')
    def ndjson(self, request):
        request.response_ndjson()
        for i in range(3):
            request.send_ndjson({'n': i})
        request.response_stream_end()
        return w.DEFERRED

    @w.api('/ndjson-types', 'GET')
    def ndjson_types(self, request):
        request.response_ndjson()
        request.send_ndjson({'s': 'héllo', 'n': 42, 'f': 3.5})
        request.send_ndjson([1, 2, [3, 4]])
        request.send_ndjson({'nested': {'a': [True, False, None]}})
        request.response_stream_end()
        return w.DEFERRED

    @w.api('/chunk', 'GET')
    def chunk(self, request):
        request.response_stream(content_type='text/plain')
        for i in range(3):
            request.send_chunk(f'chunk{i}\n'.encode())
        request.response_stream_end()
        return w.DEFERRED


# --- dispatcher ---------------------------------------------------------

class NdjsonStreamWorker(w.Worker):
    """One worker, many concurrent NDJSON streams emitted over time via
    on_idle — proves non-blocking streaming (no worker per stream)."""

    def setup(self):
        self._streams = []  # [request, remaining_records, next_emit_time]

    @w.api('/stream', 'GET')
    def stream(self, request):
        n = int((request.query or {}).get('n', 5))
        request.response_ndjson()
        records = [{'i': i, 'sq': i * i} for i in range(n)]
        self._streams.append([request, records, time.time()])
        return w.DEFERRED

    def on_idle(self):
        now = time.time()
        keep = []
        for st in self._streams:
            request, records, next_emit = st
            if now >= next_emit and records:
                request.send_ndjson(records.pop(0))
                st[2] = now + 0.01
            if records:
                keep.append(st)
            else:
                request.response_stream_end()
        self._streams = keep


class IntegrationDispatcher(w.Dispatcher):
    @w.sync('/health')
    def health(self, client, path_params):
        client.respond({'status': 'ok'})


# --- one-time server harness -------------------------------------------

class _Server:
    """Builds the dispatcher, binds servers on ephemeral ports, starts pools,
    and pumps the event loop in a daemon thread (run() minus signal setup)."""

    def __init__(self):
        self.dispatcher = IntegrationDispatcher(
            servers=[
                {'name': 'public', 'address': '127.0.0.1', 'port': 0},
                {'name': 'internal', 'address': '127.0.0.1', 'port': 0},
            ],
            pools=[
                w.WorkerPool(EchoWorker, num_workers=2, routes=['/echo']),
                w.WorkerPool(SlowWorker, num_workers=4, routes=['/slow']),
                w.WorkerPool(
                    DeferredWorker, num_workers=1, routes=['/deferred'],
                    heartbeat_interval=0.02),
                w.WorkerPool(
                    MatrixWorker, num_workers=1, routes=['/pub'],
                    servers=['public']),
                w.WorkerPool(
                    MatrixWorker, num_workers=1, routes=['/int'],
                    servers=['internal']),
                w.WorkerPool(
                    StreamWorker, num_workers=2,
                    routes=['/sse', '/ndjson', '/chunk', '/ndjson-types']),
                w.WorkerPool(
                    NdjsonStreamWorker, num_workers=1, routes=['/stream'],
                    heartbeat_interval=0.01),
            ],
        )
        self.dispatcher.SELECT_TIMEOUT = 0.02
        # build servers (run() minus signals) and discover the bound ports
        import uhttp.server as _srv
        self.dispatcher._http_servers = [
            (name, _srv.HttpServer(**kw))
            for name, kw in self.dispatcher._server_specs]
        self.ports = {
            name: server.socket.getsockname()[1]
            for name, server in self.dispatcher._http_servers}
        for pool in self.dispatcher._pools:
            pool.start(self.dispatcher._response_queue)
        self.dispatcher._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        # wait until workers are alive
        deadline = time.time() + 5
        while time.time() < deadline:
            if all(p.alive_count == p.num_workers
                    for p in self.dispatcher._pools):
                break
            time.sleep(0.05)

    def _pump(self):
        while self.dispatcher._running:
            self.dispatcher._wait_events()

    def url(self, server='public'):
        return f'http://127.0.0.1:{self.ports[server]}'

    def stop(self):
        self.dispatcher._running = False
        self._thread.join(timeout=3)
        for pool in self.dispatcher._pools:
            pool.shutdown(timeout=3)
        for _name, server in self.dispatcher._http_servers:
            server.close()


_SERVER = None


def setUpModule():
    global _SERVER
    _SERVER = _Server()


def tearDownModule():
    if _SERVER is not None:
        _SERVER.stop()


def _raw_get(port, path, timeout=3):
    """Raw HTTP/1.1 GET; read until the server closes (for live streams)."""
    conn = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    conn.sendall(
        f"GET {path} HTTP/1.1\r\nHost: x\r\n"
        f"Connection: close\r\n\r\n".encode())
    conn.settimeout(timeout)
    chunks = []
    try:
        while True:
            b = conn.recv(4096)
            if not b:
                break
            chunks.append(b)
    except socket.timeout:
        pass
    finally:
        conn.close()
    return b''.join(chunks)


def _consume_stream(url, path, accept, read, timeout=5):
    """Drive an event-mode client over a worker stream (connection-close
    framed) and collect what `read` returns on each EVENT_DATA.

    accept/read are method names: ('accept_ndjson', 'read_record') for NDJSON
    or ('accept_body_streaming', 'read_buffer') for raw chunks.
    """
    items = []
    client = HttpClient(url, event_mode=True)
    try:
        client.get(path, stream=True)
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise AssertionError("stream did not complete in time")
            r, wr, _ = select.select(
                client.read_sockets, client.write_sockets, [], 0.5)
            event = client.process_events(r, wr)
            if event == EVENT_HEADERS:
                getattr(client, accept)()
            elif event == EVENT_DATA:
                items.append(getattr(client, read)())
            elif event == EVENT_COMPLETE:
                return client.status, items
            elif event == EVENT_ERROR:
                raise AssertionError(f"stream error: {client.error}")
    finally:
        client.close()


def _drive_ndjson_streams(clients, timeout=10):
    """Drive N event-mode NDJSON clients in ONE select loop (async).

    Returns {index: [records]}. Each client is fully drained per wakeup so
    several records arriving in one recv don't stall behind the select.
    """
    results = {i: [] for i in range(len(clients))}
    for c in clients:
        c.get('/stream', query={'n': 10}, stream=True)
    done = set()
    deadline = time.time() + timeout
    while len(done) < len(clients):
        if time.time() > deadline:
            raise AssertionError(
                f"only {len(done)}/{len(clients)} streams completed")
        read_socks, write_socks = [], []
        for c in clients:
            read_socks += c.read_sockets
            write_socks += c.write_sockets
        r, wr, _ = select.select(read_socks, write_socks, [], 0.5)
        for i, c in enumerate(clients):
            if i in done:
                continue
            while True:  # drain all events currently available for this client
                event = c.process_events(r, wr)
                if event == EVENT_HEADERS:
                    c.accept_ndjson()
                elif event == EVENT_DATA:
                    results[i].append(c.read_record())
                elif event == EVENT_COMPLETE:
                    done.add(i)
                    break
                elif event == EVENT_ERROR:
                    raise AssertionError(f"client {i}: {c.error}")
                else:
                    break
    return results


def _drive(clients, timeout=5):
    """Drive multiple uhttp-clients to completion via one select loop.

    Returns {index: HttpResponse}.
    """
    responses = {}
    deadline = time.time() + timeout
    while len(responses) < len(clients):
        if time.time() > deadline:
            raise AssertionError(
                f"only {len(responses)}/{len(clients)} responded in time")
        read_socks, write_socks = [], []
        for c in clients:
            read_socks += c.read_sockets
            write_socks += c.write_sockets
        r, wr, _ = select.select(read_socks, write_socks, [], 0.5)
        for i, c in enumerate(clients):
            if i not in responses:
                resp = c.process_events(r, wr)
                if resp is not None:
                    responses[i] = resp
    return responses


# --- tests --------------------------------------------------------------

class TestRoundTrip(unittest.TestCase):

    def test_get_echo(self):
        with HttpClient(_SERVER.url()) as c:
            resp = c.get('/echo', query={'a': '1'}).wait(timeout=5)
            self.assertEqual(resp.status, 200)
            body = resp.json()
            self.assertEqual(body['method'], 'GET')
            self.assertEqual(body['query'], {'a': '1'})

    def test_post_echo(self):
        with HttpClient(_SERVER.url()) as c:
            resp = c.post('/echo', json={'hello': 'world'}).wait(timeout=5)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()['data'], {'hello': 'world'})

    def test_sync_route(self):
        with HttpClient(_SERVER.url()) as c:
            resp = c.get('/health').wait(timeout=5)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()['status'], 'ok')

    def test_unknown_path_404(self):
        with HttpClient(_SERVER.url()) as c:
            resp = c.get('/nope').wait(timeout=5)
            self.assertEqual(resp.status, 404)


class TestConcurrency(unittest.TestCase):

    def test_parallel_slow_requests_run_in_parallel(self):
        # 4 workers, 4 concurrent 0.3s requests -> ~0.3s wall, not 1.2s serial
        clients = [HttpClient(_SERVER.url()) for _ in range(4)]
        try:
            for c in clients:
                c.get('/slow')
            t0 = time.time()
            responses = _drive(clients, timeout=5)
            elapsed = time.time() - t0
            self.assertEqual(len(responses), 4)
            for resp in responses.values():
                self.assertEqual(resp.status, 200)
            self.assertLess(elapsed, 0.9, "slow requests did not parallelize")
            workers = {resp.json()['worker'] for resp in responses.values()}
            self.assertEqual(len(workers), 4, "not spread across all workers")
        finally:
            for c in clients:
                c.close()

    def test_deferred_single_worker_non_blocking(self):
        # ONE deferred worker, 5 concurrent requests -> all ~0.3s in parallel
        # (DEFERRED keeps the worker accepting while requests are pending)
        clients = [HttpClient(_SERVER.url()) for _ in range(5)]
        try:
            for c in clients:
                c.get('/deferred')
            t0 = time.time()
            responses = _drive(clients, timeout=5)
            elapsed = time.time() - t0
            self.assertEqual(len(responses), 5)
            for resp in responses.values():
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.json()['deferred'])
            self.assertLess(elapsed, 1.0, "deferred requests serialized")
        finally:
            for c in clients:
                c.close()

    def test_dispatcher_not_blocked_by_busy_workers(self):
        # while 4 slow requests occupy all workers, a sync /health must still
        # return promptly — proves the dispatcher loop isn't blocked
        slow = [HttpClient(_SERVER.url()) for _ in range(4)]
        health = HttpClient(_SERVER.url())
        try:
            for c in slow:
                c.get('/slow')
            time.sleep(0.05)  # let the slow requests occupy the workers
            t0 = time.time()
            resp = health.get('/health').wait(timeout=2)
            health_elapsed = time.time() - t0
            self.assertEqual(resp.status, 200)
            self.assertLess(health_elapsed, 0.2, "dispatcher was blocked")
            _drive(slow, timeout=5)  # drain the slow ones
        finally:
            for c in slow:
                c.close()
            health.close()


class TestStreaming(unittest.TestCase):

    def test_sse(self):
        raw = _raw_get(_SERVER.ports['public'], '/sse')
        self.assertIn(b'200', raw.split(b'\r\n', 1)[0])
        self.assertEqual(raw.count(b'event: tick'), 3)
        self.assertEqual(raw.count(b'data:'), 3)

    def test_ndjson(self):
        # consumed with uhttp-client event mode (accept_ndjson -> decoded)
        status, records = _consume_stream(
            _SERVER.url('public'), '/ndjson',
            'accept_ndjson', 'read_record')
        self.assertEqual(status, 200)
        self.assertEqual(records, [{'n': 0}, {'n': 1}, {'n': 2}])

    def test_chunked_response(self):
        # raw chunk stream consumed with accept_body_streaming -> read_buffer
        status, chunks = _consume_stream(
            _SERVER.url('public'), '/chunk',
            'accept_body_streaming', 'read_buffer')
        self.assertEqual(status, 200)
        body = b''.join(chunks)
        for i in range(3):
            self.assertIn(f'chunk{i}'.encode(), body)

    def test_ndjson_many_records_in_order(self):
        status, records = _consume_stream(
            _SERVER.url('public'), '/stream?n=25',
            'accept_ndjson', 'read_record', timeout=10)
        self.assertEqual(status, 200)
        self.assertEqual(len(records), 25)
        self.assertEqual(records, [{'i': i, 'sq': i * i} for i in range(25)])

    def test_ndjson_data_fidelity(self):
        # mixed types survive the workers send_ndjson -> client read_record path
        status, records = _consume_stream(
            _SERVER.url('public'), '/ndjson-types',
            'accept_ndjson', 'read_record')
        self.assertEqual(status, 200)
        self.assertEqual(records, [
            {'s': 'héllo', 'n': 42, 'f': 3.5},
            [1, 2, [3, 4]],
            {'nested': {'a': [True, False, None]}},
        ])


class TestAsyncStreams(unittest.TestCase):
    """Multiple NDJSON streams consumed concurrently in one select loop."""

    def test_parallel_ndjson_streams(self):
        # 4 concurrent streams, ONE worker serving them non-blocking via
        # on_idle, all driven by a single client-side select loop
        clients = [HttpClient(_SERVER.url(), event_mode=True)
                   for _ in range(4)]
        try:
            t0 = time.time()
            results = _drive_ndjson_streams(clients, timeout=10)
            elapsed = time.time() - t0
            expected = [{'i': i, 'sq': i * i} for i in range(10)]
            for i in range(4):
                self.assertEqual(results[i], expected,
                                 f"stream {i} records wrong/out of order")
            # one worker multiplexes all 4 -> not 4x a single stream's time
            self.assertLess(elapsed, 1.5, "streams did not multiplex")
        finally:
            for c in clients:
                c.close()


class TestMultiServer(unittest.TestCase):

    def test_request_server_public(self):
        with HttpClient(_SERVER.url('public')) as c:
            resp = c.get('/echo').wait(timeout=5)
            self.assertEqual(resp.json()['server'], 'public')

    def test_request_server_internal(self):
        with HttpClient(_SERVER.url('internal')) as c:
            resp = c.get('/echo').wait(timeout=5)
            self.assertEqual(resp.json()['server'], 'internal')


class TestMatrix(unittest.TestCase):

    def test_public_only_endpoint(self):
        with HttpClient(_SERVER.url('public')) as c:
            self.assertEqual(c.get('/pub').wait(timeout=5).status, 200)
        with HttpClient(_SERVER.url('internal')) as c:
            self.assertEqual(c.get('/pub').wait(timeout=5).status, 404)

    def test_internal_only_endpoint(self):
        with HttpClient(_SERVER.url('internal')) as c:
            self.assertEqual(c.get('/int').wait(timeout=5).status, 200)
        with HttpClient(_SERVER.url('public')) as c:
            self.assertEqual(c.get('/int').wait(timeout=5).status, 404)


if __name__ == '__main__':
    unittest.main()
