"""Tests for WorkerPool management."""

import unittest
import time
import multiprocessing as mp

from uhttp.workers import Worker, WorkerPool, api, MSG_HEARTBEAT


class DummyWorker(Worker):
    @api('/test', 'GET')
    def test_handler(self, request):
        return {'ok': True}


class _DeadWorker:
    """Stub for a worker process that exited with a given code.

    Models mp.Process: after close() is called, is_alive() raises — so a
    test that closes a parked slot would surface the landmine.
    """

    def __init__(self, exitcode):
        self.exitcode = exitcode
        self.closed = False

    def is_alive(self):
        if self.closed:
            raise ValueError("process object is closed")
        return False

    def join(self, timeout=None):
        pass

    def close(self):
        self.closed = True

    def kill(self):
        pass


class _AliveWorker:
    """Stub for a running worker process."""

    exitcode = None

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass

    def close(self):
        pass

    def kill(self):
        pass


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

    def test_alive_count_empty(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        # not started yet
        self.assertEqual(pool.alive_count, 0)

    def test_alive_count_running(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        response_queue = mp.Queue()
        pool.start(response_queue)
        time.sleep(0.2)
        self.assertEqual(pool.alive_count, 2)
        pool.shutdown(timeout=3)

    def test_alive_count_in_status(self):
        pool = WorkerPool(DummyWorker, num_workers=2)
        response_queue = mp.Queue()
        pool.start(response_queue)
        time.sleep(0.2)
        self.assertEqual(pool.status()['alive_count'], 2)
        pool.shutdown(timeout=3)


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

    def test_degraded_since_set_once(self):
        """_degraded_since records first entry, not every later restart."""
        pool = WorkerPool(
            DummyWorker, num_workers=1,
            max_restarts=2, restart_window=60)
        response_queue = mp.Queue()
        pool.start(response_queue)
        for _ in range(2):
            pool.workers[0].kill()
            pool.workers[0].join(timeout=2)
            pool.check_workers()
            time.sleep(0.1)
        self.assertTrue(pool.is_degraded)
        first_since = pool._degraded_since
        self.assertIsNotNone(first_since)
        # further restarts must not move the timestamp
        for _ in range(2):
            pool.workers[0].kill()
            pool.workers[0].join(timeout=2)
            pool.check_workers()
            time.sleep(0.1)
        self.assertEqual(pool._degraded_since, first_since)
        pool.shutdown(timeout=3)


class TestWorkerPoolRecovery(unittest.TestCase):
    """Auto-recovery from degraded via recovery_interval."""

    def test_default_is_sticky(self):
        """recovery_interval=None never auto-recovers (no workers needed)."""
        pool = WorkerPool(DummyWorker, num_workers=0)
        pool._degraded = True
        pool._degraded_since = time.time() - 1000
        pool._restart_times = [time.time()]
        pool.check_workers()
        self.assertTrue(pool.is_degraded)
        self.assertEqual(len(pool._restart_times), 1)  # not cleared

    def test_recovery_clears_degraded(self):
        pool = WorkerPool(
            DummyWorker, num_workers=0, recovery_interval=1)
        pool._degraded = True
        pool._degraded_since = time.time() - 2  # elapsed
        pool._restart_times = [time.time() - 0.5]
        pool.check_workers()
        self.assertFalse(pool.is_degraded)
        self.assertIsNone(pool._degraded_since)
        self.assertEqual(pool._restart_times, [])

    def test_recovery_not_yet_elapsed(self):
        pool = WorkerPool(
            DummyWorker, num_workers=0, recovery_interval=100)
        now = time.time()
        pool._degraded = True
        pool._degraded_since = now  # just entered
        pool._restart_times = [now]
        pool.check_workers()
        self.assertTrue(pool.is_degraded)
        self.assertIsNotNone(pool._degraded_since)

    def test_recovery_interval_property(self):
        pool = WorkerPool(DummyWorker, recovery_interval=42)
        self.assertEqual(pool.recovery_interval, 42)
        pool2 = WorkerPool(DummyWorker)
        self.assertIsNone(pool2.recovery_interval)


class TestWorkerPoolParking(unittest.TestCase):
    """Permanent-failure parking via permanent_failure_exitcode."""

    def _pool(self, num_workers=1, **kw):
        pool = WorkerPool(DummyWorker, num_workers=num_workers, **kw)
        pool._last_seen = {i: time.time() for i in range(num_workers)}
        pool._current_request = {i: None for i in range(num_workers)}
        pool._start_worker = lambda i: self._spawned.append(i)
        self._spawned = []
        return pool

    def test_parks_matching_exitcode(self):
        pool = self._pool(permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(42)]
        result = pool.check_workers()
        self.assertIn(0, pool._parked)
        self.assertEqual(self._spawned, [])          # not restarted
        self.assertEqual(pool._restart_times, [])    # not counted
        self.assertEqual(result, [(0, 'parked exit=42', 42)])

    def test_restarts_other_exitcode(self):
        pool = self._pool(permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(-9)]   # OOM kill — not permanent
        pool.check_workers()
        self.assertNotIn(0, pool._parked)
        self.assertEqual(self._spawned, [0])
        self.assertEqual(len(pool._restart_times), 1)

    def test_disabled_never_parks(self):
        pool = self._pool()  # permanent_failure_exitcode=None
        pool.workers = [_DeadWorker(42)]
        pool.check_workers()
        self.assertEqual(pool._parked, set())
        self.assertEqual(self._spawned, [0])

    def test_skips_already_parked_slot(self):
        pool = self._pool(permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(42)]
        pool._parked = {0}
        result = pool.check_workers()
        self.assertEqual(result, [])
        self.assertEqual(self._spawned, [])

    def test_parked_slot_not_closed(self):
        # the landmine guard: parking must join() but not close(), else
        # is_alive() raises later in alive_count/status/shutdown
        pool = self._pool(permanent_failure_exitcode=42)
        worker = _DeadWorker(42)
        pool.workers = [worker]
        pool.check_workers()
        self.assertFalse(worker.closed)
        # none of these raise (would if the slot had been closed)
        self.assertEqual(pool.alive_count, 0)
        pool.status()
        pool._control_queues = []
        pool.shutdown(timeout=0)

    def test_alive_count_excludes_parked(self):
        pool = self._pool(num_workers=2, permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(42), _AliveWorker()]
        pool._parked = {0}
        self.assertEqual(pool.alive_count, 1)

    def test_status_marks_parked(self):
        pool = self._pool(num_workers=2, permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(42), _AliveWorker()]
        pool._parked = {0}
        st = pool.status()
        self.assertEqual(st['parked_count'], 1)
        self.assertTrue(st['workers'][0]['parked'])
        self.assertFalse(st['workers'][1]['parked'])

    def test_all_parked_sets_degraded(self):
        pool = self._pool(permanent_failure_exitcode=42)
        pool.workers = [_DeadWorker(42)]
        pool.check_workers()
        self.assertTrue(pool.is_degraded)

    def test_recovery_skipped_when_all_parked(self):
        pool = self._pool(
            permanent_failure_exitcode=42, recovery_interval=1)
        pool.workers = [_DeadWorker(42)]
        pool._parked = {0}
        pool._degraded = True
        pool._degraded_since = time.time() - 5   # elapsed
        pool._restart_times = [time.time()]
        pool.check_workers()
        self.assertTrue(pool.is_degraded)   # not recovered — nothing to retry


if __name__ == '__main__':
    unittest.main()
