"""Tests for Worker request handling and routing."""

import time
import unittest
import multiprocessing as mp

from uhttp.workers import (
    Worker, Request, Response, api, RejectRequest, DEFERRED,
    Logger, LOG_DEBUG, LOG_INFO, LOG_WARNING, LOG_ERROR,
    MSG_RESPONSE, MSG_HEARTBEAT,
    MSG_SSE_OPEN, MSG_SSE_EVENT, MSG_SSE_CLOSE,
    CTL_DISCONNECT)


class SimpleWorker(Worker):
    @api('/items', 'GET')
    def list_items(self, request):
        return {'items': [1, 2, 3]}

    @api('/items', 'POST')
    def create_item(self, request):
        return {'created': request.data}, 201

    @api('/item/{id:int}', 'GET')
    def get_item(self, request):
        return {'id': request.path_params['id']}

    @api('/item/{id:int}', 'DELETE')
    def delete_item(self, request):
        return {'deleted': request.path_params['id']}

    @api('/fail', 'GET')
    def fail(self, request):
        raise ValueError("test error")


class TestWorkerHandleRequest(unittest.TestCase):

    def setUp(self):
        queues = [mp.Queue() for _ in range(3)]
        self.worker = SimpleWorker(0, *queues)
        self.worker._build_routes()

    def test_get_items(self):
        req = Request(1, 'GET', '/items')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'items': [1, 2, 3]})

    def test_post_items(self):
        req = Request(2, 'POST', '/items', data={'name': 'test'})
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.data, {'created': {'name': 'test'}})

    def test_get_item_with_param(self):
        req = Request(3, 'GET', '/item/42')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'id': 42})

    def test_delete_item(self):
        req = Request(4, 'DELETE', '/item/7')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'deleted': 7})

    def test_not_found(self):
        req = Request(5, 'GET', '/nonexistent')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 404)
        self.assertIn('error', resp.data)

    def test_method_not_allowed(self):
        req = Request(6, 'PUT', '/items')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 405)
        self.assertIn('Allow', resp.headers)

    def test_handler_exception(self):
        req = Request(7, 'GET', '/fail')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 500)
        self.assertIn('error', resp.data)
        self.assertIn('test error', resp.data['error'])

    def test_response_has_request_id(self):
        req = Request(99, 'GET', '/items')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.request_id, 99)

    def test_int_param_invalid(self):
        req = Request(8, 'GET', '/item/abc')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 404)

    def test_method_not_allowed_on_param_route(self):
        req = Request(9, 'POST', '/item/42')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 405)


class CheckWorker(Worker):
    """Worker with do_check that rejects unauthorized requests."""

    def do_check(self, request):
        token = request.cookies.get('session')
        if not token:
            return {'error': 'unauthorized'}, 401

    @api('/secret', 'GET')
    def secret(self, request):
        return {'data': 'secret'}


class RejectWorker(Worker):
    """Worker with do_check that raises RejectRequest."""

    def do_check(self, request):
        raise RejectRequest()

    @api('/data', 'GET')
    def data(self, request):
        return {'data': 'ok'}


class FailCheckWorker(Worker):
    """Worker with do_check that raises unexpected exception."""

    def do_check(self, request):
        raise RuntimeError("check failed")

    @api('/data', 'GET')
    def data(self, request):
        return {'data': 'ok'}


class TestWorkerDoCheck(unittest.TestCase):

    def test_do_check_reject(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = CheckWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/secret')
        resp = worker._handle_request(req)
        self.assertEqual(resp.status, 401)

    def test_do_check_pass(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = CheckWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/secret',
            headers={'cookie': 'session=abc123'})
        resp = worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'data': 'secret'})

    def test_do_check_reject_request(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = RejectWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/data')
        resp = worker._handle_request(req)
        self.assertEqual(resp.status, 403)

    def test_do_check_exception(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = FailCheckWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/data')
        resp = worker._handle_request(req)
        self.assertEqual(resp.status, 500)


class TestRequestCookies(unittest.TestCase):

    def test_cookies_parsed(self):
        req = Request(1, 'GET', '/',
            headers={'cookie': 'session=abc; user=john'})
        self.assertEqual(req.cookies, {'session': 'abc', 'user': 'john'})

    def test_cookies_empty(self):
        req = Request(1, 'GET', '/')
        self.assertEqual(req.cookies, {})

    def test_cookies_no_cookie_header(self):
        req = Request(1, 'GET', '/', headers={'content-type': 'text/html'})
        self.assertEqual(req.cookies, {})

    def test_cookies_cached(self):
        req = Request(1, 'GET', '/',
            headers={'cookie': 'a=1'})
        c1 = req.cookies
        c2 = req.cookies
        self.assertIs(c1, c2)


class TestWorkerMatchRoute(unittest.TestCase):

    def setUp(self):
        queues = [mp.Queue() for _ in range(3)]
        self.worker = SimpleWorker(0, *queues)
        self.worker._build_routes()

    def test_match_sets_path_params(self):
        req = Request(1, 'GET', '/item/42')
        handler = self.worker._match_route(req)
        self.assertIsNotNone(handler)
        self.assertEqual(req.path_params, {'id': 42})

    def test_no_match_returns_none(self):
        req = Request(1, 'GET', '/nonexistent')
        handler = self.worker._match_route(req)
        self.assertIsNone(handler)

    def test_wrong_method_returns_none(self):
        req = Request(1, 'PATCH', '/items')
        handler = self.worker._match_route(req)
        self.assertIsNone(handler)


class ConfigWorker(Worker):
    config_received = None

    def on_config(self, config):
        ConfigWorker.config_received = config


class TestWorkerProcessControl(unittest.TestCase):

    def test_stop_via_run(self):
        """Worker stops when None is received on control queue."""
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        control_queue.put(None)
        worker = SimpleWorker(0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_config_via_run(self):
        """Worker receives config message."""
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        control_queue.put(('CONFIG', {'key': 'value'}))
        control_queue.put(None)  # stop after config
        worker = ConfigWorker(0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


class SetupWorker(Worker):
    def setup(self):
        # signal setup was called by sending a message
        self._response_queue.put(('SETUP_DONE', self.worker_id, None))


class TestWorkerRunLoop(unittest.TestCase):

    def test_processes_request_and_stops(self):
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = SimpleWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        # put request — control stop will be sent after we see response
        request_queue.put(Request(1, 'GET', '/items'))
        worker.start()
        # wait for response
        found_response = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                msg = response_queue.get(timeout=0.5)
            except Exception:
                continue
            if msg[0] == MSG_RESPONSE:
                _, req_id, resp = msg
                self.assertEqual(req_id, 1)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.data, {'items': [1, 2, 3]})
                found_response = True
                break
        self.assertTrue(found_response)
        control_queue.put(None)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_heartbeat_on_idle(self):
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = SimpleWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        worker.start()
        # wait for at least one heartbeat
        msg = response_queue.get(timeout=3)
        self.assertEqual(msg[0], MSG_HEARTBEAT)
        self.assertIsNone(msg[1])  # pool_name (not set)
        self.assertEqual(msg[2], 0)  # worker_id
        self.assertIsNone(msg[3])  # no request_id
        # stop
        control_queue.put(None)
        worker.join(timeout=5)

    def test_setup_called(self):
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = SetupWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        worker.start()
        # wait for setup signal
        msg = response_queue.get(timeout=5)
        self.assertEqual(msg[0], 'SETUP_DONE')
        control_queue.put(None)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


class DeferredWorker(Worker):
    @api('/defer', 'POST')
    def defer(self, request):
        return DEFERRED

    @api('/normal', 'GET')
    def normal(self, request):
        return {'ok': True}


class TestDeferredResponse(unittest.TestCase):

    def setUp(self):
        queues = [mp.Queue() for _ in range(3)]
        self.worker = DeferredWorker(0, *queues)
        self.worker._build_routes()

    def test_deferred_returns_none(self):
        req = Request(1, 'POST', '/defer')
        resp = self.worker._handle_request(req)
        self.assertIsNone(resp)

    def test_normal_returns_response(self):
        req = Request(2, 'GET', '/normal')
        resp = self.worker._handle_request(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, 200)

    def test_request_respond(self):
        response_queue = mp.Queue()
        req = Request(1, 'POST', '/defer')
        req._response_queue = response_queue
        req.respond(data={'done': True}, status=201)
        msg = response_queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_RESPONSE)
        self.assertEqual(msg[1], 1)
        self.assertEqual(msg[2].status, 201)
        self.assertEqual(msg[2].data, {'done': True})

    def test_deferred_run_loop(self):
        """DEFERRED handler does not put response on queue."""
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = DeferredWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        request_queue.put(Request(1, 'POST', '/defer'))
        worker.start()
        # collect messages — should get heartbeat but no MSG_RESPONSE
        deadline = time.time() + 2
        messages = []
        while time.time() < deadline:
            try:
                msg = response_queue.get(timeout=0.2)
                messages.append(msg)
            except Exception:
                continue
        control_queue.put(None)
        worker.join(timeout=5)
        response_msgs = [m for m in messages if m[0] == MSG_RESPONSE]
        self.assertEqual(len(response_msgs), 0)
        heartbeat_msgs = [m for m in messages if m[0] == MSG_HEARTBEAT]
        self.assertGreater(len(heartbeat_msgs), 0)


class TestKeepAlive(unittest.TestCase):

    def test_keep_alive_sends_heartbeat(self):
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = SimpleWorker(0, request_queue, control_queue, response_queue)
        worker._current_request_id = 42
        worker.keep_alive()
        msg = response_queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_HEARTBEAT)
        self.assertEqual(msg[2], 0)  # worker_id
        self.assertEqual(msg[3], 42)  # request_id


class TestLoggerLevelChecks(unittest.TestCase):

    def test_debug_level(self):
        log = Logger('test', mp.Queue(), level=LOG_DEBUG)
        self.assertTrue(log.is_debug)
        self.assertTrue(log.is_info)
        self.assertTrue(log.is_warning)
        self.assertTrue(log.is_error)

    def test_info_level(self):
        log = Logger('test', mp.Queue(), level=LOG_INFO)
        self.assertFalse(log.is_debug)
        self.assertTrue(log.is_info)
        self.assertTrue(log.is_warning)
        self.assertTrue(log.is_error)

    def test_warning_level(self):
        log = Logger('test', mp.Queue(), level=LOG_WARNING)
        self.assertFalse(log.is_debug)
        self.assertFalse(log.is_info)
        self.assertTrue(log.is_warning)
        self.assertTrue(log.is_error)

    def test_error_level(self):
        log = Logger('test', mp.Queue(), level=LOG_ERROR)
        self.assertFalse(log.is_debug)
        self.assertFalse(log.is_info)
        self.assertFalse(log.is_warning)
        self.assertTrue(log.is_error)


class SSEWorker(Worker):
    @api('/events', 'GET')
    def events(self, request):
        request.response_stream()
        return DEFERRED


class TestSSERequest(unittest.TestCase):
    """Test SSE methods on Request — message format and queue delivery."""

    def setUp(self):
        self.queue = mp.Queue()
        self.req = Request(10, 'GET', '/events')
        self.req._response_queue = self.queue

    def test_response_stream(self):
        self.req.response_stream(
            content_type='text/event-stream',
            headers={'X-Custom': '1'},
            cookies={'sid': 'abc'})
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_OPEN)
        self.assertEqual(msg[1], 10)
        self.assertEqual(msg[2], 'text/event-stream')
        self.assertEqual(msg[3], {'X-Custom': '1'})
        self.assertEqual(msg[4], {'sid': 'abc'})

    def test_response_stream_defaults(self):
        self.req.response_stream()
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_OPEN)
        self.assertIsNone(msg[2])  # content_type
        self.assertIsNone(msg[3])  # headers
        self.assertIsNone(msg[4])  # cookies

    def test_send_event(self):
        self.req.send_event(
            data={'count': 1}, event='update',
            event_id='5', retry=3000)
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_EVENT)
        self.assertEqual(msg[1], 10)
        self.assertEqual(msg[2], {'count': 1})
        self.assertEqual(msg[3], 'update')
        self.assertEqual(msg[4], '5')
        self.assertEqual(msg[5], 3000)

    def test_send_event_data_only(self):
        self.req.send_event(data='hello')
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[2], 'hello')
        self.assertIsNone(msg[3])  # event
        self.assertIsNone(msg[4])  # event_id
        self.assertIsNone(msg[5])  # retry

    def test_send_chunk(self):
        self.req.send_chunk(b'raw data')
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_EVENT)
        self.assertEqual(msg[2], b'raw data')
        # send_chunk uses None for event/event_id/retry
        self.assertIsNone(msg[3])
        self.assertIsNone(msg[4])
        self.assertIsNone(msg[5])

    def test_response_stream_end(self):
        self.req.response_stream_end()
        msg = self.queue.get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_CLOSE)
        self.assertEqual(msg[1], 10)

    def test_sse_handler_returns_deferred(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = SSEWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/events')
        req._response_queue = queues[2]
        resp = worker._handle_request(req)
        self.assertIsNone(resp)
        # response_stream() should have been sent
        msg = queues[2].get(timeout=1)
        self.assertEqual(msg[0], MSG_SSE_OPEN)


class TrackingDisconnectWorker(Worker):
    def on_disconnect(self, request_id):
        # signal disconnect via response queue
        self._response_queue.put(('DISCONNECTED', request_id))


class TestSSEWorkerDisconnect(unittest.TestCase):
    """Test on_disconnect via control queue."""

    def test_disconnect_control_message(self):
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        control_queue.put((CTL_DISCONNECT, 42))
        control_queue.put(None)  # stop
        worker = TrackingDisconnectWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.1
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        # find DISCONNECTED message in response queue
        found = False
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                msg = response_queue.get(timeout=0.2)
            except Exception:
                continue
            if msg[0] == 'DISCONNECTED':
                self.assertEqual(msg[1], 42)
                found = True
                break
        self.assertTrue(found)


class PausingWorker(Worker):
    """Worker that pauses after first request."""

    @api('/work', 'POST')
    def work(self, request):
        self.pause()
        return {'paused': True}

    @api('/resume', 'POST')
    def do_resume(self, request):
        self.resume()
        return {'resumed': True}


class TestWorkerPauseResume(unittest.TestCase):

    def test_pause_resume_flags(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = SimpleWorker(0, *queues)
        self.assertTrue(worker._accepting)
        worker.pause()
        self.assertFalse(worker._accepting)
        worker.resume()
        self.assertTrue(worker._accepting)

    def test_paused_worker_skips_request(self):
        """Paused worker does not pick up requests from queue."""
        request_queue = mp.Queue()
        control_queue = mp.Queue()
        response_queue = mp.Queue()
        worker = PausingWorker(
            0, request_queue, control_queue, response_queue)
        worker.heartbeat_interval = 0.2
        # first request will pause the worker
        request_queue.put(Request(1, 'POST', '/work'))
        # second request should stay in queue (worker paused)
        request_queue.put(Request(2, 'POST', '/work'))
        worker.start()
        # wait for first response
        deadline = time.time() + 5
        responses = []
        while time.time() < deadline:
            try:
                msg = response_queue.get(timeout=0.3)
            except Exception:
                # after getting first response, give worker time
                # to NOT pick up second request
                if responses:
                    break
                continue
            if msg[0] == MSG_RESPONSE:
                responses.append(msg)
                if len(responses) >= 1:
                    # wait a bit to confirm no second response
                    time.sleep(0.5)
                    break
        control_queue.put(None)
        worker.join(timeout=5)
        # only one response — second request stayed in queue
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][1], 1)  # request_id
        # second request still in queue
        self.assertFalse(request_queue.empty())
        req = request_queue.get_nowait()
        self.assertEqual(req.request_id, 2)


if __name__ == '__main__':
    unittest.main()
