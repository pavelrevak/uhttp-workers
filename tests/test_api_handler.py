"""Tests for ApiHandler and Worker HANDLERS integration."""

import unittest
import multiprocessing as mp

from uhttp.workers import Worker, ApiHandler, Request, api


class UserHandler(ApiHandler):
    PATTERN = '/api/user'

    @api('', 'GET')
    def list_users(self, request):
        return {'users': list(self.worker.users.values())}

    @api('/{id:int}', 'GET')
    def get_user(self, request):
        uid = request.path_params['id']
        if uid not in self.worker.users:
            return {'error': 'Not found'}, 404
        return self.worker.users[uid]

    @api('/{id:int}', 'DELETE')
    def delete_user(self, request):
        uid = request.path_params['id']
        if uid not in self.worker.users:
            return {'error': 'Not found'}, 404
        del self.worker.users[uid]
        return {'deleted': uid}


class OrderHandler(ApiHandler):
    PATTERN = '/api/order'

    @api('', 'GET')
    def list_orders(self, request):
        return {'orders': []}

    @api('/{id:int}', 'GET')
    def get_order(self, request):
        return {'id': request.path_params['id']}


class HandlerWorker(Worker):
    HANDLERS = [UserHandler, OrderHandler]

    @api('/api/health', 'GET')
    def health(self, request):
        return {'status': 'ok'}


class TestApiHandlerRoutes(unittest.TestCase):

    def setUp(self):
        queues = [mp.Queue() for _ in range(3)]
        self.worker = HandlerWorker(0, *queues)
        self.worker.users = {1: {'id': 1, 'name': 'Alice'}}
        self.worker._build_routes()

    def test_routes_collected(self):
        patterns = {r[0] for r in self.worker._routes}
        self.assertIn('/api/user', patterns)
        self.assertIn('/api/user/{id:int}', patterns)
        self.assertIn('/api/order', patterns)
        self.assertIn('/api/order/{id:int}', patterns)
        self.assertIn('/api/health', patterns)

    def test_handler_list(self):
        req = Request(1, 'GET', '/api/user')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'users': [{'id': 1, 'name': 'Alice'}]})

    def test_handler_get_with_param(self):
        req = Request(2, 'GET', '/api/user/1')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'id': 1, 'name': 'Alice'})

    def test_handler_not_found(self):
        req = Request(3, 'GET', '/api/user/999')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 404)

    def test_handler_delete(self):
        req = Request(4, 'DELETE', '/api/user/1')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'deleted': 1})
        self.assertNotIn(1, self.worker.users)

    def test_handler_method_not_allowed(self):
        req = Request(5, 'POST', '/api/user/1')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 405)

    def test_other_handler(self):
        req = Request(6, 'GET', '/api/order/42')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'id': 42})

    def test_worker_direct_route(self):
        req = Request(7, 'GET', '/api/health')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'status': 'ok'})

    def test_no_route(self):
        req = Request(8, 'GET', '/api/nonexistent')
        resp = self.worker._handle_request(req)
        self.assertEqual(resp.status, 404)


class TestApiHandlerAccess(unittest.TestCase):

    def test_handler_has_worker_reference(self):
        queues = [mp.Queue() for _ in range(3)]
        worker = HandlerWorker(0, *queues)
        worker._build_routes()
        for handler in worker._handlers:
            self.assertIs(handler.worker, worker)


class TestApiHandlerInheritance(unittest.TestCase):

    def test_inherited_handler_routes(self):

        class BaseHandler(ApiHandler):
            PATTERN = '/api/base'

            @api('/ping', 'GET')
            def ping(self, request):
                return {'pong': True}

        class ExtendedHandler(BaseHandler):
            PATTERN = '/api/ext'

            @api('/extra', 'GET')
            def extra(self, request):
                return {'extra': True}

        class TestWorker(Worker):
            HANDLERS = [ExtendedHandler]

        queues = [mp.Queue() for _ in range(3)]
        worker = TestWorker(0, *queues)
        worker._build_routes()
        patterns = {r[0] for r in worker._routes}
        # ExtendedHandler.PATTERN is /api/ext
        self.assertIn('/api/ext/extra', patterns)
        # inherited ping should also use ExtendedHandler.PATTERN
        self.assertIn('/api/ext/ping', patterns)


class TestEmptyPattern(unittest.TestCase):

    def test_handler_empty_pattern(self):

        class RootHandler(ApiHandler):
            @api('/status', 'GET')
            def status(self, request):
                return {'ok': True}

        class TestWorker(Worker):
            HANDLERS = [RootHandler]

        queues = [mp.Queue() for _ in range(3)]
        worker = TestWorker(0, *queues)
        worker._build_routes()
        req = Request(1, 'GET', '/status')
        resp = worker._handle_request(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data, {'ok': True})


if __name__ == '__main__':
    unittest.main()
