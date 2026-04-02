"""uhttp-workers: Multi-process API server built on uhttp-server

Provides dispatcher/worker architecture for handling large volumes
of API requests using multiple processes.
"""

import sys as _sys
import os as _os
import traceback as _traceback
import time as _time
import queue as _queue
import signal as _signal
import select as _select
import multiprocessing as _mp

import uhttp.server as _uhttp_server


# Message types for response queue
MSG_RESPONSE = 'RESPONSE'
MSG_HEARTBEAT = 'HEARTBEAT'
MSG_LOG = 'LOG'

# Worker control messages
CTL_STOP = 'STOP'
CTL_CONFIG = 'CONFIG'

# Log levels
LOG_CRITICAL = 50
LOG_ERROR = 40
LOG_WARNING = 30
LOG_INFO = 20
LOG_DEBUG = 10

LOG_LEVEL_NAMES = {
    LOG_CRITICAL: 'CRITICAL',
    LOG_ERROR: 'ERROR',
    LOG_WARNING: 'WARNING',
    LOG_INFO: 'INFO',
    LOG_DEBUG: 'DEBUG',
}

# Syslog priority prefixes for systemd-journald
_LOG_SYSLOG_PREFIX = {
    LOG_CRITICAL: '<2>',
    LOG_ERROR: '<3>',
    LOG_WARNING: '<4>',
    LOG_INFO: '<6>',
    LOG_DEBUG: '<7>',
}

# ANSI color codes for terminal output
_LOG_ANSI_COLOR = {
    LOG_CRITICAL: '\033[1;31m',  # bold red
    LOG_ERROR: '\033[31m',       # red
    LOG_WARNING: '\033[33m',     # yellow
    LOG_INFO: '\033[0m',         # default
    LOG_DEBUG: '\033[2m',        # dim
}
_ANSI_RESET = '\033[0m'

_DIR_INDEX = 'index.html'


# Exceptions

class ApiException(Exception):
    """Base exception for uhttp-workers."""


class RejectRequest(ApiException):
    """Raised in do_check() to reject request (response already sent)."""


# Route decorator

def api(pattern, *methods):
    """Decorator to register a method as API endpoint handler on Worker.

    Args:
        pattern: URL pattern with optional parameters (e.g., '/user/{id:int}')
        *methods: HTTP methods to accept (e.g., 'GET', 'POST'). None = all.
    """
    def decorator(func):
        func._api_pattern = pattern
        func._api_methods = list(methods) if methods else None
        return func
    return decorator


def sync(pattern, *methods):
    """Decorator to register a method as sync handler on Dispatcher.

    Sync handlers run directly in dispatcher process.
    Use for lightweight, fast responses only.

    Args:
        pattern: URL pattern with optional parameters (e.g., '/health')
        *methods: HTTP methods to accept (e.g., 'GET', 'POST'). None = all.
    """
    def decorator(func):
        func._sync_pattern = pattern
        func._sync_methods = list(methods) if methods else None
        return func
    return decorator


# Type converters for path parameters

_TYPE_CONVERTERS = {
    'str': str,
    'int': int,
    'float': float,
}


def _parse_param(pattern_part):
    """Parse parameter pattern like {name} or {name:type}.

    Returns:
        Tuple (param_name, converter_func) or None if not a parameter.
    """
    if not (pattern_part.startswith('{') and pattern_part.endswith('}')):
        return None
    inner = pattern_part[1:-1]
    if ':' in inner:
        name, type_name = inner.split(':', 1)
        converter = _TYPE_CONVERTERS.get(type_name)
        if converter is None:
            raise ValueError(f"Unknown type converter: {type_name}")
        return name, converter
    return inner, str


def _match_pattern(pattern, path):
    """Match URL path against pattern with parameters.

    Args:
        pattern: Pattern string (e.g., '/user/{id:int}')
        path: URL path (e.g., '/user/42')

    Returns:
        Dict of path parameters if match, None otherwise.
    """
    pattern_parts = [p for p in pattern.split('/') if p]
    path_parts = [p for p in path.split('/') if p]
    if len(pattern_parts) != len(path_parts):
        return None
    path_params = {}
    for pattern_part, path_part in zip(pattern_parts, path_parts):
        param = _parse_param(pattern_part)
        if param:
            name, converter = param
            try:
                path_params[name] = converter(path_part)
            except (ValueError, TypeError):
                return None
        elif pattern_part != path_part:
            return None
    return path_params


def _match_prefix(prefix_pattern, path):
    """Match URL path against prefix pattern with glob support.

    Supports '**' wildcard at the end of pattern.

    Args:
        prefix_pattern: Pattern like '/api/users/**'
        path: URL path

    Returns:
        True if path matches prefix pattern.
    """
    if prefix_pattern.endswith('/**'):
        prefix = prefix_pattern[:-3]
        prefix_parts = [p for p in prefix.split('/') if p]
        path_parts = [p for p in path.split('/') if p]
        return path_parts[:len(prefix_parts)] == prefix_parts
    # exact match
    return path == prefix_pattern


# Request/Response objects passed through queues

class Request:
    """HTTP request data passed from dispatcher to worker via queue.

    Attributes:
        request_id: Internal ID for dispatcher/worker pairing.
        method: HTTP method (e.g., 'GET', 'POST').
        path: URL path (e.g., '/api/user/42').
        query: Parsed query parameters dict, or None.
        data: Parsed body — dict (JSON), bytes (binary), or None.
        headers: Request headers dict.
        content_type: Content-Type header value, or None.
        path_params: Path parameters filled by worker router.
    """

    __slots__ = (
        'request_id', 'method', 'path', 'query',
        'data', 'headers', 'content_type', 'path_params')

    def __init__(
            self, request_id, method, path, query=None,
            data=None, headers=None, content_type=None):
        self.request_id = request_id
        self.method = method
        self.path = path
        self.query = query
        self.data = data
        self.headers = headers or {}
        self.content_type = content_type
        self.path_params = {}


class Response:
    """HTTP response data passed from worker to dispatcher via queue.

    Attributes:
        request_id: Matches the originating Request.
        status: HTTP status code.
        data: Response body — dict (JSON), bytes (binary), or None.
        headers: Response headers dict, or None.
    """

    __slots__ = ('request_id', 'status', 'data', 'headers')

    def __init__(
            self, request_id, data=None, status=200, headers=None):
        self.request_id = request_id
        self.status = status
        self.data = data
        self.headers = headers


# API Handler

class ApiHandler:
    """Base class for grouping API endpoints under a common URL prefix.

    Subclass and set PATTERN as the URL prefix. Define handlers with @api
    decorator. Handlers access the worker via self.worker.

    Attributes:
        PATTERN: URL prefix prepended to all @api patterns in this class.
        worker: Reference to the Worker instance that owns this handler.
    """

    PATTERN = ''

    def __init__(self, worker):
        self.worker = worker


# Logger

class Logger:
    """Logger that sends log records to dispatcher via response queue.

    Supports both %-style and {}-style message formatting.
    Messages below the configured level are not sent to queue.

    Attributes:
        name: Logger name (included in log output).
        level: Minimum log level.
    """

    def __init__(self, name, queue, level=LOG_WARNING):
        self.name = name
        self.level = level
        self._queue = queue

    def _log(self, level, msg, *args, **kwargs):
        if level >= self.level:
            try:
                message = msg % args if args else msg
                message = message.format(**kwargs) if kwargs else message
            except (TypeError, KeyError, IndexError, ValueError):
                message = f"{msg} {args} {kwargs}"
            self._queue.put((MSG_LOG, self.name, level, message))

    def critical(self, msg, *args, **kwargs):
        self._log(LOG_CRITICAL, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log(LOG_ERROR, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(LOG_WARNING, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(LOG_INFO, msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._log(LOG_DEBUG, msg, *args, **kwargs)


# Worker

class Worker(_mp.Process):
    """Base worker process. Subclass and define handlers with @api decorator.

    Handlers can be defined directly on the worker or in separate
    ApiHandler classes listed in HANDLERS. Uses select()-based event loop
    for multiplexing request queue, control queue, and custom file descriptors.

    Attributes:
        HANDLERS: List of ApiHandler subclasses with grouped endpoints.
        worker_id: Unique index of this worker within its pool.
        heartbeat_interval: Seconds between heartbeats when idle.
        kwargs: Extra arguments from WorkerPool, accessible in setup().
        log: Logger instance for sending log messages to dispatcher.
    """

    HANDLERS = []

    def __init__(
            self, worker_id, request_queue, control_queue,
            response_queue, heartbeat_interval=1,
            log_level=LOG_WARNING, pool_name=None, **kwargs):
        """Initialize worker process.

        Args:
            worker_id: Unique index of this worker within its pool.
            request_queue: Queue for receiving Request objects from dispatcher.
            control_queue: Per-worker queue for stop signals and config updates.
            response_queue: Shared queue for sending responses and heartbeats
                back to dispatcher.
            heartbeat_interval: Seconds between heartbeats when idle.
            log_level: Minimum log level for worker logger.
            pool_name: Name of the pool this worker belongs to.
            **kwargs: Extra arguments accessible via self.kwargs in setup().
        """
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.pool_name = pool_name
        self.heartbeat_interval = heartbeat_interval
        self.kwargs = kwargs
        self._request_queue = request_queue
        self._control_queue = control_queue
        self._response_queue = response_queue
        self.log = Logger(
            f'{type(self).__name__}[{worker_id}]',
            response_queue, level=log_level)
        self._routes = []
        self._handlers = []
        self._readers = {}
        self._writers = {}
        self._running = True

    def _build_routes(self):
        """Collect @api decorated methods from worker and HANDLERS."""
        # routes from worker itself
        for klass in type(self).__mro__:
            for name, val in vars(klass).items():
                if callable(val) and hasattr(val, '_api_pattern'):
                    bound = getattr(self, name)
                    self._routes.append((
                        val._api_pattern,
                        val._api_methods,
                        bound))
        # routes from handler classes
        for handler_cls in self.HANDLERS:
            handler = handler_cls(self)
            self._handlers.append(handler)
            prefix = handler_cls.PATTERN.rstrip('/')
            for klass in handler_cls.__mro__:
                if klass is ApiHandler or klass is object:
                    continue
                for name, val in vars(klass).items():
                    if callable(val) and hasattr(val, '_api_pattern'):
                        full_pattern = prefix + val._api_pattern
                        bound = getattr(handler, name)
                        self._routes.append((
                            full_pattern,
                            val._api_methods,
                            bound))

    def _match_route(self, request):
        """Find matching handler for request, or None."""
        for pattern, methods, handler in self._routes:
            if methods and request.method not in methods:
                continue
            path_params = _match_pattern(pattern, request.path)
            if path_params is not None:
                request.path_params = path_params
                return handler
        return None

    def register_reader(self, fd, callback):
        """Register file-like object for read events in worker select loop.

        Args:
            fd: Any object with fileno() (socket, serial port, pipe, ...).
            callback: Called with fd when readable: callback(fd).
        """
        self._readers[fd] = callback

    def unregister_reader(self, fd):
        """Remove file-like object from read events."""
        self._readers.pop(fd, None)

    def register_writer(self, fd, callback):
        """Register file-like object for write events in worker select loop.

        Only register when there is data to send, unregister when buffer
        is empty to avoid spinning in select.

        Args:
            fd: Any object with fileno() (socket, serial port, pipe, ...).
            callback: Called with fd when writable: callback(fd).
        """
        self._writers[fd] = callback

    def unregister_writer(self, fd):
        """Remove file-like object from write events."""
        self._writers.pop(fd, None)

    def setup(self):
        """Called once when worker process starts.

        Override to initialize resources (database connections, models, etc.).
        Extra kwargs from WorkerPool are available as self.kwargs.
        """

    def on_idle(self):
        """Called on each heartbeat interval when no request arrived.

        Override for periodic background processing.
        """

    def on_config(self, config):
        """Called when dispatcher sends configuration update via control queue.

        Args:
            config: Configuration dict sent by pool.send_config().
        """

    def _process_control(self):
        """Process all pending control messages."""
        while True:
            try:
                msg = self._control_queue.get_nowait()
            except _queue.Empty:
                return
            except (EOFError, OSError):
                self._running = False
                return
            if msg is None or (isinstance(msg, tuple) and msg[0] == CTL_STOP):
                self._running = False
                return
            if isinstance(msg, tuple) and msg[0] == CTL_CONFIG:
                self.on_config(msg[1])

    def _handle_request(self, request):
        """Route and handle a single request, return Response."""
        handler = self._match_route(request)
        if handler is None:
            # check if path matches but method doesn't
            for pattern, methods, _ in self._routes:
                if _match_pattern(pattern, request.path) is not None:
                    return Response(
                        request.request_id,
                        data={'error': 'Method not allowed'},
                        status=405,
                        headers={'Allow': ', '.join(methods)})
            return Response(
                request.request_id,
                data={'error': 'Not found'},
                status=404)
        try:
            result = handler(request)
            if isinstance(result, tuple):
                data, status = result
            else:
                data, status = result, 200
            return Response(request.request_id, data=data, status=status)
        except Exception as err:
            self.log.error(
                "%s %s: %s\n%s",
                request.method, request.path, err,
                _traceback.format_exc())
            return Response(
                request.request_id,
                data={'error': str(err)},
                status=500)

    def run(self):
        """Worker main loop using select for multiplexing."""
        _signal.signal(_signal.SIGTERM, lambda *_: None)
        _signal.signal(_signal.SIGINT, lambda *_: None)
        try:
            self._build_routes()
            self.setup()
        except Exception:
            self._response_queue.put(
                (MSG_LOG, f'{type(self).__name__}[{self.worker_id}]',
                 LOG_CRITICAL,
                 f"setup() failed:\n{_traceback.format_exc()}"))
            return
        req_reader = self._request_queue._reader
        ctl_reader = self._control_queue._reader
        while self._running:
            read_fds = [req_reader, ctl_reader] + list(self._readers)
            write_fds = list(self._writers)
            readable, writable, _ = _select.select(
                read_fds, write_fds, [], self.heartbeat_interval)
            if not self._running:
                break
            if not readable and not writable:
                # timeout — heartbeat, orphan check, idle hook
                if _os.getppid() == 1:
                    break
                self._response_queue.put(
                    (MSG_HEARTBEAT, self.pool_name, self.worker_id, None))
                self.on_idle()
                continue
            # control messages
            if ctl_reader in readable:
                self._process_control()
                if not self._running:
                    break
            # custom writers
            for fd in writable:
                if fd in self._writers:
                    self._writers[fd](fd)
            # custom readers
            for fd in readable:
                if fd in self._readers:
                    self._readers[fd](fd)
            # request from dispatcher
            if req_reader in readable:
                try:
                    request = self._request_queue.get_nowait()
                except _queue.Empty:
                    continue
                except (EOFError, OSError):
                    break
                self._response_queue.put(
                    (MSG_HEARTBEAT, self.pool_name, self.worker_id, request.request_id))
                response = self._handle_request(request)
                self._response_queue.put(
                    (MSG_RESPONSE, request.request_id, response))


# Worker Pool

class WorkerPool:
    """Manages a group of workers of the same type.

    Handles worker lifecycle: start, health monitoring, restart, shutdown.
    """

    def __init__(
            self, worker_class, num_workers=1, routes=None,
            timeout=30, stuck_timeout=60, heartbeat_interval=1,
            log_level=LOG_WARNING, max_restarts=10,
            restart_window=300, queue_warning=100, **kwargs):
        """Initialize worker pool.

        Args:
            worker_class: Worker subclass to instantiate.
            num_workers: Number of worker processes.
            routes: Prefix patterns for dispatcher routing
                (e.g., ['/api/users/**']). None = fallback pool.
            timeout: Request timeout in seconds (504 response).
            stuck_timeout: Max seconds without heartbeat before kill.
            heartbeat_interval: Seconds between worker heartbeats.
            log_level: Minimum log level for worker loggers.
            max_restarts: Max restarts per restart_window before degraded.
            restart_window: Time window for counting restarts (seconds).
            queue_warning: Log warning when queue size exceeds this value.
                Set to 0 to disable.
            **kwargs: Extra arguments passed to worker constructor.
        """
        self.worker_class = worker_class
        self.num_workers = num_workers
        self.routes = routes
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.log_level = log_level
        self.stuck_timeout = stuck_timeout
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.queue_warning = queue_warning
        self.kwargs = kwargs
        self.name = worker_class.__name__
        self.request_queue = _mp.Queue()
        self.workers = []
        self._control_queues = []
        self._last_seen = {}
        self._current_request = {}
        self._restart_times = []
        self._degraded = False
        self._response_queue = None

    def start(self, response_queue):
        """Start all workers in this pool.

        Args:
            response_queue: Shared response queue for all pools.
        """
        self._response_queue = response_queue
        for i in range(self.num_workers):
            self._start_worker(i)

    def _start_worker(self, index):
        """Start or restart a single worker."""
        control_queue = _mp.Queue()
        worker = self.worker_class(
            worker_id=index,
            request_queue=self.request_queue,
            control_queue=control_queue,
            response_queue=self._response_queue,
            heartbeat_interval=self.heartbeat_interval,
            log_level=self.log_level,
            pool_name=self.name,
            **self.kwargs)
        worker.start()
        if index < len(self.workers):
            self.workers[index] = worker
            self._control_queues[index] = control_queue
        else:
            self.workers.append(worker)
            self._control_queues.append(control_queue)
        self._last_seen[index] = _time.time()
        self._current_request[index] = None

    def update_heartbeat(self, worker_id, request_id=None):
        """Update last seen time for a worker."""
        self._last_seen[worker_id] = _time.time()
        self._current_request[worker_id] = request_id

    def check_workers(self):
        """Check worker health, restart dead or stuck workers.

        Returns:
            List of (worker_id, reason) tuples for restarted workers.
        """
        restarted = []
        now = _time.time()
        # clean old restart times
        self._restart_times = [
            t for t in self._restart_times
            if now - t < self.restart_window]
        for i, worker in enumerate(self.workers):
            reason = None
            if not worker.is_alive():
                reason = f"died exit={worker.exitcode}"
            elif now - self._last_seen.get(i, 0) > self.stuck_timeout:
                reason = "stuck"
                worker.kill()
            if reason:
                try:
                    worker.join(timeout=1)
                    worker.close()
                except Exception:
                    pass
                self._restart_times.append(now)
                if len(self._restart_times) >= self.max_restarts:
                    self._degraded = True
                self._start_worker(i)
                restarted.append((i, reason))
        return restarted

    def matches(self, path):
        """Check if path matches any of this pool's route patterns.

        Args:
            path: URL path to match.

        Returns:
            True if path matches, or pool is fallback (routes=None).
        """
        if self.routes is None:
            return True  # default/fallback pool
        for route in self.routes:
            if _match_prefix(route, path):
                return True
        return False

    def broadcast(self, msg):
        """Send message to all workers via their control queues.

        Args:
            msg: Message to send (None for stop, tuple for config).
        """
        for control_queue in self._control_queues:
            control_queue.put(msg)

    def send_config(self, config):
        """Send configuration update to all workers.

        Args:
            config: Dict received by worker's on_config() method.
        """
        self.broadcast((CTL_CONFIG, config))

    def shutdown(self, timeout=5):
        """Stop all workers gracefully, kill after timeout.

        Args:
            timeout: Max seconds to wait for workers to finish.
        """
        self.broadcast(None)
        deadline = _time.time() + timeout
        for worker in self.workers:
            remaining = max(0, deadline - _time.time())
            worker.join(timeout=remaining)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=1)

    @property
    def is_degraded(self):
        return self._degraded

    @property
    def pending_count(self):
        try:
            return self.request_queue.qsize()
        except NotImplementedError:
            return 0

    def status(self):
        """Return pool status dict for monitoring.

        Returns:
            Dict with name, degraded, queue_size, and per-worker info.
        """
        now = _time.time()
        return {
            'name': self.name,
            'degraded': self._degraded,
            'queue_size': self.pending_count,
            'workers': [
                {
                    'id': i,
                    'alive': w.is_alive(),
                    'last_seen': round(now - self._last_seen.get(i, 0), 1),
                    'current_request': self._current_request.get(i),
                }
                for i, w in enumerate(self.workers)
            ],
        }


# Pending request tracking

class _PendingRequest:
    __slots__ = ('client', 'timestamp', 'pool')

    def __init__(self, client, pool):
        self.client = client
        self.timestamp = _time.time()
        self.pool = pool


# Dispatcher

class Dispatcher:
    """Main dispatcher process — HTTP server, routing, worker management.

    Handles static files and @sync routes directly in the main process.
    Routes API requests to worker pools via queues. Uses select()-based
    event loop for multiplexing HTTP sockets, response queue, and custom
    file descriptors.
    """

    SELECT_TIMEOUT = 1

    def __init__(
            self, port=8080, address='0.0.0.0', pools=None,
            static_routes=None, shutdown_timeout=10,
            max_pending=1000, **kwargs):
        """Initialize dispatcher.

        Args:
            port: Listen port.
            address: Listen address.
            pools: List of WorkerPool instances.
            static_routes: Dict of URL prefix -> filesystem path.
            shutdown_timeout: Seconds to wait for workers on shutdown.
            max_pending: Max pending requests before rejecting (503).
            ssl_context: Optional ssl.SSLContext for HTTPS.
            **kwargs: Extra arguments passed to HttpServer.
        """
        self._port = port
        self._address = address
        self._pools = pools or []
        self._static_routes = {}
        if static_routes:
            for prefix, path in static_routes.items():
                self._static_routes[prefix] = _os.path.abspath(
                    _os.path.expanduser(path))
        self._shutdown_timeout = shutdown_timeout
        self._max_pending = max_pending
        self._server_kwargs = kwargs
        self._http_server = None
        self._response_queue = _mp.Queue()
        self._pending = {}
        self._next_request_id = 0
        self._sync_routes = []
        self._readers = {}
        self._writers = {}
        self._log_is_tty = _sys.stderr.isatty()
        self._running = False
        self._build_sync_routes()

    def _build_sync_routes(self):
        """Collect @sync decorated methods and build sync route table."""
        for klass in type(self).__mro__:
            for name, val in vars(klass).items():
                if callable(val) and hasattr(val, '_sync_pattern'):
                    bound = getattr(self, name)
                    self._sync_routes.append((
                        val._sync_pattern,
                        val._sync_methods,
                        bound))

    def register_reader(self, fd, callback):
        """Register file-like object for read events in dispatcher select loop.

        Args:
            fd: Any object with fileno() (socket, serial port, pipe, ...).
            callback: Called with fd when readable: callback(fd).
        """
        self._readers[fd] = callback

    def unregister_reader(self, fd):
        """Remove file-like object from read events."""
        self._readers.pop(fd, None)

    def register_writer(self, fd, callback):
        """Register file-like object for write events in dispatcher select loop.

        Only register when there is data to send, unregister when buffer
        is empty to avoid spinning in select.

        Args:
            fd: Any object with fileno() (socket, serial port, pipe, ...).
            callback: Called with fd when writable: callback(fd).
        """
        self._writers[fd] = callback

    def unregister_writer(self, fd):
        """Remove file-like object from write events."""
        self._writers.pop(fd, None)

    def on_response(self, response, pending):
        """Called after response is sent to client.

        Override to post-process, e.g., forward data to another pool.

        Args:
            response: Response object from worker.
            pending: _PendingRequest with client and pool reference.
        """

    def on_idle(self):
        """Called on each select timeout when no events arrived.

        Override for periodic background processing in dispatcher.
        """

    def do_check(self, client):
        """Validation hook called before dispatching request to worker pool.

        Override for API key validation, auth, rate limiting, etc.
        Send error response and raise RejectRequest to skip dispatch.

        Args:
            client: HttpConnection from uhttp-server.
        """

    def _serve_static(self, client):
        """Try to serve static file. Returns True if served."""
        path = client.path
        for prefix, base_path in self._static_routes.items():
            if path.startswith(prefix):
                rel_path = path[len(prefix):]
                file_path = _os.path.normpath(
                    _os.path.join(base_path, rel_path))
                # path traversal protection
                if not (file_path.startswith(base_path + _os.sep)
                        or file_path == base_path):
                    continue
                if _os.path.isdir(file_path):
                    file_path = _os.path.join(file_path, _DIR_INDEX)
                if _os.path.isfile(file_path):
                    client.respond_file(file_path)
                    return True
        return False

    def _handle_sync(self, client):
        """Try sync route handlers. Returns True if handled."""
        for pattern, methods, handler in self._sync_routes:
            if methods and client.method not in methods:
                continue
            path_params = _match_pattern(pattern, client.path)
            if path_params is not None:
                handler(client, path_params)
                return True
        return False

    def _find_pool(self, path):
        """Find matching worker pool for path, or fallback pool."""
        default_pool = None
        for pool in self._pools:
            if pool.routes is None:
                default_pool = pool
                continue
            if pool.matches(path):
                return pool
        return default_pool

    def _dispatch_to_pool(self, client):
        """Send request to matching worker pool."""
        pool = self._find_pool(client.path)
        if pool is None:
            client.respond({'error': 'Not found'}, status=404)
            return
        if pool.is_degraded:
            client.respond(
                {'error': 'Service unavailable'}, status=503)
            return
        if len(self._pending) >= self._max_pending:
            client.respond(
                {'error': 'Too many requests'}, status=503)
            return
        request_id = self._next_request_id
        self._next_request_id += 1
        self._pending[request_id] = _PendingRequest(client, pool)
        pool.request_queue.put(Request(
            request_id=request_id,
            method=client.method,
            path=client.path,
            query=client.query,
            data=client.data,
            headers=dict(client.headers),
            content_type=client.content_type))

    def _http_request(self, client):
        """Process incoming HTTP request."""
        # 1. static files
        if self._serve_static(client):
            return
        # 2. sync handlers
        if self._handle_sync(client):
            return
        # 3. auth/validation check
        try:
            self.do_check(client)
        except RejectRequest:
            return
        except Exception:
            client.respond({'error': 'Internal server error'}, status=500)
            return
        # 4. dispatch to worker pool
        self._dispatch_to_pool(client)

    def _process_response(self, msg):
        """Process a single message from response queue."""
        msg_type = msg[0]
        if msg_type == MSG_HEARTBEAT:
            _, pool_name, worker_id, request_id = msg
            for pool in self._pools:
                if pool.name == pool_name:
                    pool.update_heartbeat(worker_id, request_id)
                    break
        elif msg_type == MSG_LOG:
            _, name, level, message = msg
            self.on_log(name, level, message)
        elif msg_type == MSG_RESPONSE:
            _, request_id, response = msg
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.client.respond(
                    response.data,
                    status=response.status,
                    headers=response.headers)
                self.on_response(response, pending)

    def _process_responses(self):
        """Process all pending messages from response queue."""
        while True:
            try:
                msg = self._response_queue.get_nowait()
            except _queue.Empty:
                return

            self._process_response(msg)

    def _expire_pending(self):
        """Timeout expired pending requests."""
        now = _time.time()
        expired = [
            rid for rid, pending in self._pending.items()
            if now - pending.timestamp > pending.pool.timeout]
        for request_id in expired:
            pending = self._pending.pop(request_id)
            self.on_log(
                pending.pool.name, LOG_WARNING,
                f"request {request_id} timed out after "
                f"{pending.pool.timeout}s")
            pending.client.respond(
                {'error': 'Request timeout'}, status=504)

    def _check_all_workers(self):
        """Check health of all worker pools and queue sizes."""
        for pool in self._pools:
            restarted = pool.check_workers()
            for worker_id, reason in restarted:
                self._on_worker_restarted(pool, worker_id, reason)
            if pool.queue_warning:
                qsize = pool.pending_count
                if qsize >= pool.queue_warning:
                    self.on_log(
                        pool.name, LOG_WARNING,
                        f"queue size {qsize} exceeds "
                        f"threshold {pool.queue_warning}")

    def on_log(self, name, level, message):
        """Called when a worker sends a log message.

        Override to customize log output or forward to logging framework.
        Default prints to stderr.

        Args:
            name: Logger name (e.g., 'MyWorker[0]').
            level: Log level (LOG_DEBUG..LOG_CRITICAL).
            message: Formatted log message string.
        """
        level_name = LOG_LEVEL_NAMES.get(level, str(level))
        if self._log_is_tty:
            color = _LOG_ANSI_COLOR.get(level, '')
            print(f"{color}{level_name:8s} {name:20s} {message}{_ANSI_RESET}",
                file=_sys.stderr)
        else:
            prefix = _LOG_SYSLOG_PREFIX.get(level, '')
            print(f"{prefix}{level_name:8s} {name:20s} {message}",
                file=_sys.stderr)

    def _on_worker_restarted(self, pool, worker_id, reason):
        """Called when a worker is restarted.

        Default logs the event. Override to customize.
        """
        self.on_log(
            f'{pool.name}[{worker_id}]', LOG_ERROR,
            f"worker restarted: {reason}")

    def _sigterm(self, _signo, _stack_frame):
        self._running = False

    def _wait_events(self):
        """Single iteration of the main event loop."""
        waiting_sockets = self._http_server.read_sockets + [
            self._response_queue._reader] + list(self._readers)
        write_sockets = (self._http_server.write_sockets
            + list(self._writers))
        read_events, write_events, _ = _select.select(
            waiting_sockets, write_sockets, [], self.SELECT_TIMEOUT)
        # process responses from workers
        if self._response_queue._reader in read_events:
            read_events = [
                s for s in read_events
                if s is not self._response_queue._reader]
            self._process_responses()
        # custom writers
        for fd in write_events:
            if fd in self._writers:
                self._writers[fd](fd)
        # custom readers
        for fd in read_events:
            if fd in self._readers:
                self._readers[fd](fd)
        # filter custom fds before passing to http server
        http_read = [s for s in read_events if s not in self._readers]
        http_write = [s for s in write_events if s not in self._writers]
        # process HTTP events
        if http_read or http_write:
            client = self._http_server.process_events(
                http_read, http_write)
            if client:
                self._http_request(client)
        # periodic maintenance (on timeout — no events)
        if not read_events and not write_events:
            self._check_all_workers()
            self.on_idle()
        # always check expired requests
        self._expire_pending()

    def run(self):
        """Start dispatcher and all worker pools.

        Blocks until SIGTERM/SIGINT, then performs graceful shutdown.
        """
        self._http_server = _uhttp_server.HttpServer(
            address=self._address,
            port=self._port,
            **self._server_kwargs)
        self._running = True
        # start all pools
        for pool in self._pools:
            pool.start(self._response_queue)
        _signal.signal(_signal.SIGTERM, self._sigterm)
        _signal.signal(_signal.SIGINT, self._sigterm)
        try:
            while self._running:
                self._wait_events()
        finally:
            self._shutdown()

    def _shutdown(self):
        """Graceful shutdown."""
        # stop accepting connections
        self._http_server.close()
        # drain remaining responses
        deadline = _time.time() + self._shutdown_timeout
        while self._pending and _time.time() < deadline:
            try:
                msg = self._response_queue.get(timeout=0.1)
                self._process_response(msg)
            except _queue.Empty:
                pass
        # respond 503 to remaining pending
        for pending in self._pending.values():
            try:
                pending.client.respond(
                    {'error': 'Server shutting down'}, status=503)
            except Exception:
                pass
        self._pending.clear()
        # shutdown all pools
        for pool in self._pools:
            pool.shutdown(timeout=self._shutdown_timeout)
