"""Tests for @api and @sync decorators."""

import unittest
import multiprocessing as mp

from uhttp.workers import api, sync, Worker, Dispatcher


class TestApiDecorator(unittest.TestCase):

    def test_sets_pattern(self):
        @api('/users')
        def handler(self, request):
            pass
        self.assertEqual(handler._api_pattern, '/users')

    def test_default_methods(self):
        @api('/users')
        def handler(self, request):
            pass
        self.assertIsNone(handler._api_methods)

    def test_custom_methods(self):
        @api('/users', 'POST', 'PUT')
        def handler(self, request):
            pass
        self.assertEqual(handler._api_methods, ['POST', 'PUT'])

    def test_preserves_function(self):
        @api('/users')
        def handler(self, request):
            return 'result'
        self.assertEqual(handler(None, None), 'result')

    def test_pattern_with_params(self):
        @api('/user/{id:int}', 'GET', 'DELETE')
        def handler(self, request):
            pass
        self.assertEqual(handler._api_pattern, '/user/{id:int}')
        self.assertEqual(handler._api_methods, ['GET', 'DELETE'])


class TestSyncDecorator(unittest.TestCase):

    def test_sets_pattern(self):
        @sync('/health')
        def handler(self, client, path_params):
            pass
        self.assertEqual(handler._sync_pattern, '/health')

    def test_default_methods(self):
        @sync('/health')
        def handler(self, client, path_params):
            pass
        self.assertIsNone(handler._sync_methods)

    def test_custom_methods(self):
        @sync('/webhook', 'POST')
        def handler(self, client, path_params):
            pass
        self.assertEqual(handler._sync_methods, ['POST'])


class TestWorkerBuildRoutes(unittest.TestCase):

    def test_collects_api_methods(self):

        class TestWorker(Worker):
            @api('/items', 'GET')
            def list_items(self, request):
                return {'items': []}

            @api('/item/{id:int}', 'GET', 'DELETE')
            def item_detail(self, request):
                return {}

        queues = [mp.Queue() for _ in range(3)]
        worker = TestWorker(0, *queues)
        worker._build_routes()
        patterns = {r[0] for r in worker._routes}
        self.assertIn('/items', patterns)
        self.assertIn('/item/{id:int}', patterns)
        self.assertEqual(len(worker._routes), 2)

    def test_no_api_methods(self):

        class EmptyWorker(Worker):
            pass

        queues = [mp.Queue() for _ in range(3)]
        worker = EmptyWorker(0, *queues)
        worker._build_routes()
        self.assertEqual(len(worker._routes), 0)

    def test_inherited_routes(self):

        class BaseWorker(Worker):
            @api('/base', 'GET')
            def base_handler(self, request):
                return {}

        class ChildWorker(BaseWorker):
            @api('/child', 'GET')
            def child_handler(self, request):
                return {}

        queues = [mp.Queue() for _ in range(3)]
        worker = ChildWorker(0, *queues)
        worker._build_routes()
        patterns = {r[0] for r in worker._routes}
        self.assertIn('/base', patterns)
        self.assertIn('/child', patterns)


class TestDispatcherBuildSyncRoutes(unittest.TestCase):

    def test_collects_sync_methods(self):

        class TestDispatcher(Dispatcher):
            @sync('/health')
            def health(self, client, path_params):
                pass

            @sync('/version')
            def version(self, client, path_params):
                pass

        d = TestDispatcher.__new__(TestDispatcher)
        d._sync_routes = []
        d._build_sync_routes = Dispatcher._build_sync_routes.__get__(d)
        d._build_sync_routes()
        patterns = {r[0] for r in d._sync_routes}
        self.assertIn('/health', patterns)
        self.assertIn('/version', patterns)
        self.assertEqual(len(d._sync_routes), 2)

    def test_no_sync_methods(self):
        d = Dispatcher.__new__(Dispatcher)
        d._sync_routes = []
        d._build_sync_routes = Dispatcher._build_sync_routes.__get__(d)
        d._build_sync_routes()
        self.assertEqual(len(d._sync_routes), 0)

    def test_inherited_sync_routes(self):

        class BaseDispatcher(Dispatcher):
            @sync('/health')
            def health(self, client, path_params):
                pass

        class ChildDispatcher(BaseDispatcher):
            @sync('/version')
            def version(self, client, path_params):
                pass

        d = ChildDispatcher.__new__(ChildDispatcher)
        d._sync_routes = []
        d._build_sync_routes = Dispatcher._build_sync_routes.__get__(d)
        d._build_sync_routes()
        patterns = {r[0] for r in d._sync_routes}
        self.assertIn('/health', patterns)
        self.assertIn('/version', patterns)


if __name__ == '__main__':
    unittest.main()
