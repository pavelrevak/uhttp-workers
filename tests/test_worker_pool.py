"""Tests for WorkerPool management."""

import unittest
import time
import multiprocessing as mp

from uhttp.workers import Worker, WorkerPool, api, MSG_HEARTBEAT


class DummyWorker(Worker):
    @api('/test', 'GET')
    def test_handler(self, request):
        return {'ok': True}


class TestWorkerPoolMatches(unittest.TestCase):

    def test_glob_match(self):
        pool = WorkerPool(DummyWorker, routes=['/api/users/**'])
        self.assertTrue(pool.matches('/api/users/123'))
        self.assertTrue(pool.matches('/api/users/123/profile'))
        self.assertFalse(pool.matches('/api/items/123'))

    def test_exact_match(self):
        pool = WorkerPool(DummyWorker, routes=['/health'])
        self.assertTrue(pool.matches('/health'))
        self.assertFalse(pool.matches('/version'))

    def test_multiple_routes(self):
        pool = WorkerPool(
            DummyWorker,
            routes=['/api/users/**', '/api/items/**'])
        self.assertTrue(pool.matches('/api/users/1'))
        self.assertTrue(pool.matches('/api/items/1'))
        self.assertFalse(pool.matches('/api/orders/1'))

    def test_fallback_pool(self):
        pool = WorkerPool(DummyWorker, routes=None)
        self.assertTrue(pool.matches('/anything'))
        self.assertTrue(pool.matches('/'))

    def test_empty_routes(self):
        pool = WorkerPool(DummyWorker, routes=[])
        self.assertFalse(pool.matches('/anything'))


class TestWorkerPoolHeartbeat(unittest.TestCase):

    def test_update_heartbeat(self):
        pool = WorkerPool(DummyWorker)
        pool._last_seen[0] = 0
        pool.update_heartbeat(0, request_id=42)
        self.assertGreater(pool._last_seen[0], 0)
        self.assertEqual(pool._current_request[0], 42)

    def test_update_heartbeat_no_request(self):
        pool = WorkerPool(DummyWorker)
        pool._last_seen[0] = 0
        pool.update_heartbeat(0)
        self.assertIsNone(pool._current_request[0])


class TestWorkerPoolStartShutdown(unittest.TestCase):

    def test_start_workers(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        response_queue = mp.Queue()
        pool.start(response_queue)
        self.assertEqual(len(pool.workers), 2)
        for w in pool.workers:
            self.assertTrue(w.is_alive())
        pool.shutdown(timeout=3)
        for w in pool.workers:
            self.assertFalse(w.is_alive())

    def test_broadcast(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        response_queue = mp.Queue()
        pool.start(response_queue)
        pool.broadcast(('CONFIG', {'key': 'val'}))
        # workers receive config — just verify they stay alive
        time.sleep(0.2)
        for w in pool.workers:
            self.assertTrue(w.is_alive())
        pool.shutdown(timeout=3)

    def test_send_config(self):
        pool = WorkerPool(DummyWorker, num_workers=1)
        response_queue = mp.Queue()
        pool.start(response_queue)
        pool.send_config({'debug': True})
        time.sleep(0.2)
        self.assertTrue(pool.workers[0].is_alive())
        pool.shutdown(timeout=3)


class TestWorkerPoolStatus(unittest.TestCase):

    def test_status_structure(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        response_queue = mp.Queue()
        pool.start(response_queue)
        status = pool.status()
        self.assertEqual(status['name'], 'DummyWorker')
        self.assertFalse(status['degraded'])
        self.assertEqual(len(status['workers']), 2)
        for w in status['workers']:
            self.assertIn('id', w)
            self.assertIn('alive', w)
            self.assertIn('last_seen', w)
            self.assertIn('current_request', w)
            self.assertTrue(w['alive'])
        pool.shutdown(timeout=3)

    def test_pending_count(self):
        pool = WorkerPool(DummyWorker, num_workers=1)
        self.assertEqual(pool.pending_count, 0)

    def test_not_degraded_by_default(self):
        pool = WorkerPool(DummyWorker)
        self.assertFalse(pool.is_degraded)


class TestWorkerPoolCheckWorkers(unittest.TestCase):

    def test_restart_dead_worker(self):
        pool = WorkerPool(DummyWorker, num_workers=1)
        response_queue = mp.Queue()
        pool.start(response_queue)
        # kill worker
        pool.workers[0].kill()
        pool.workers[0].join(timeout=2)
        self.assertFalse(pool.workers[0].is_alive())
        # check should restart
        restarted = pool.check_workers()
        self.assertEqual(len(restarted), 1)
        self.assertEqual(restarted[0][0], 0)
        self.assertIn('died', restarted[0][1])
        # new worker should be alive
        time.sleep(0.2)
        self.assertTrue(pool.workers[0].is_alive())
        pool.shutdown(timeout=3)

    def test_no_restart_healthy(self):
        pool = WorkerPool(DummyWorker, num_workers=1)
        response_queue = mp.Queue()
        pool.start(response_queue)
        time.sleep(0.2)
        restarted = pool.check_workers()
        self.assertEqual(len(restarted), 0)
        pool.shutdown(timeout=3)

    def test_degraded_after_many_restarts(self):
        pool = WorkerPool(
            DummyWorker, num_workers=1,
            max_restarts=2, restart_window=60)
        response_queue = mp.Queue()
        pool.start(response_queue)
        # simulate multiple restarts
        for _ in range(3):
            pool.workers[0].kill()
            pool.workers[0].join(timeout=2)
            pool.check_workers()
            time.sleep(0.1)
        self.assertTrue(pool.is_degraded)
        pool.shutdown(timeout=3)


if __name__ == '__main__':
    unittest.main()
