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

try:
    import resource as _resource
except ImportError:  # non-POSIX (e.g. Windows)
    _resource = None

import uhttp.server as _uhttp_server


# Message types for response queue
MSG_RESPONSE = 'RESPONSE'
MSG_HEARTBEAT = 'HEARTBEAT'
MSG_LOG = 'LOG'
MSG_SSE_OPEN = 'SSE_OPEN'
MSG_SSE_EVENT = 'SSE_EVENT'
MSG_SSE_CLOSE = 'SSE_CLOSE'
MSG_NDJSON = 'NDJSON'

# Worker control messages
CTL_STOP = 'STOP'
CTL_CONFIG = 'CONFIG'
CTL_DISCONNECT = 'DISCONNECT'

# Reasons for on_pending_removed
PENDING_COMPLETED = 'COMPLETED'
PENDING_TIMEOUT = 'TIMEOUT'
PENDING_DISCONNECTED = 'DISCONNECTED'
PENDING_STREAM_CLOSED = 'STREAM_CLOSED'
PENDING_SHUTDOWN = 'SHUTDOWN'
PENDING_WORKER_DIED = 'WORKER_DIED'

# Sentinel for deferred response
DEFERRED = object()

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
    """Raised in do_check() to reject request with custom status/data."""

    def __init__(self, data=None, status=403):
        self.data = data if data is not None else {'error': 'Rejected'}
        self.status = status


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
        remote_address: Client address as "host:port" string. Honors
            X-Forwarded-For when the connection comes from a trusted
            proxy (uhttp-server's trusted_proxies setting). None if the
            dispatcher could not resolve the address (e.g., in tests).
    """

    __slots__ = (
        'request_id', 'method', 'path', 'query',
        'data', 'headers', 'content_type', 'path_params',
        'remote_address',
        '_cookies', '_response_queue')

    def __init__(
            self, request_id, method, path, query=None,
            data=None, headers=None, content_type=None,
            remote_address=None):
        self.request_id = request_id
        self.method = method
        self.path = path
        self.query = query
        self.data = data
        self.headers = headers or {}
        self.content_type = content_type
        self.path_params = {}
        self.remote_address = remote_address
        self._cookies = None
        self._response_queue = None

    @property
    def cookies(self):
        """Cookies dict, parsed from Cookie header."""
        if self._cookies is None:
            raw = self.headers.get('cookie', '')
            self._cookies = (
                _uhttp_server.parse_cookies(raw) if raw else {})
        return self._cookies

    def respond(self, data=None, status=200, headers=None, cookies=None):
        """Send deferred response for this request.

        Use after returning DEFERRED from handler.
        """
        self._response_queue.put(
            (MSG_RESPONSE, self.request_id,
             Response(
                 self.request_id, data=data, status=status,
                 headers=headers, cookies=cookies)))

    def response_stream(self, content_type=None, headers=None, cookies=None):
        """Start streaming response.

        Use with DEFERRED — call from handler, then send_event()
        or send_chunk() later. Call response_stream_end() when done.
        """
        self._response_queue.put(
            (MSG_SSE_OPEN, self.request_id,
             content_type, headers, cookies))

    def send_chunk(self, data):
        """Send raw data chunk to stream."""
        self._response_queue.put(
            (MSG_SSE_EVENT, self.request_id, data, None, None, None))

    def send_event(self, data=None, event=None, event_id=None, retry=None):
        """Send SSE event to stream.

        Args:
            data: Event data (str, dict, list, number).
            event: Event type name.
            event_id: Event ID for client reconnection.
            retry: Reconnection time in milliseconds.
        """
        self._response_queue.put(
            (MSG_SSE_EVENT, self.request_id,
             data, event, event_id, retry))

    def response_ndjson(self, headers=None, cookies=None):
        """Start NDJSON streaming response (application/x-ndjson).

        Thin wrapper over response_stream(). Use with DEFERRED — call from
        handler, then send_ndjson() later. Call response_stream_end() to finish.
        """
        self._response_queue.put(
            (MSG_SSE_OPEN, self.request_id,
             'application/x-ndjson', headers, cookies))

    def send_ndjson(self, obj):
        """Send one JSON-serializable object as an NDJSON line.

        Args:
            obj: any JSON-serializable value (dict/list/str/int/float/bool/None)
        """
        self._response_queue.put(
            (MSG_NDJSON, self.request_id, obj))

    def response_stream_end(self):
        """End streaming response and close connection."""
        self._response_queue.put(
            (MSG_SSE_CLOSE, self.request_id))


class Response:
    """HTTP response data passed from worker to dispatcher via queue.

    Attributes:
        request_id: Matches the originating Request.
        status: HTTP status code.
        data: Response body — dict (JSON), bytes (binary), or None.
        headers: Response headers dict, or None.
        cookies: Response cookies dict, or None.
    """

    __slots__ = ('request_id', 'status', 'data', 'headers', 'cookies')

    def __init__(
            self, request_id, data=None, status=200,
            headers=None, cookies=None):
        self.request_id = request_id
        self.status = status
        self.data = data
        self.headers = headers
        self.cookies = cookies


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

    def __init__(self, name, queue=None, level=LOG_WARNING, sink=None):
        """Logger that sends to dispatcher via queue or direct callable.

        Args:
            name: Logger name.
            queue: multiprocessing.Queue (worker context).
            level: Minimum log level.
            sink: Callable(name, level, message) — used instead of queue
                (e.g., dispatcher's on_log).
        """
        self.name = name
        self.level = level
        self._queue = queue
        self._sink = sink

    @property
    def is_debug(self):
        return self.level <= LOG_DEBUG

    @property
    def is_info(self):
        return self.level <= LOG_INFO

    @property
    def is_warning(self):
        return self.level <= LOG_WARNING

    @property
    def is_error(self):
        return self.level <= LOG_ERROR

    def _log(self, level, msg, *args, **kwargs):
        if level >= self.level:
            try:
                message = msg % args if args else msg
                message = message.format(**kwargs) if kwargs else message
            except (TypeError, KeyError, IndexError, ValueError):
                message = f"{msg} {args} {kwargs}"
            if self._sink is not None:
                self._sink(self.name, level, message)
            else:
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
        LOG_NAME: Logger name template, formatted with {cls}, {worker_id},
            {pool_name}, {pid}. Default '{cls}[{worker_id}]'; override to
            customize (e.g. 'api-{pool_name}-{worker_id}'). {pid} is the
            worker process PID.
        worker_id: Unique index of this worker within its pool.
        heartbeat_interval: Seconds between heartbeats when idle.
        kwargs: Extra arguments from WorkerPool, accessible in setup().
        log: Logger instance for sending log messages to dispatcher.
    """

    HANDLERS = []
    LOG_NAME = '{cls}[{worker_id}]'

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
        self._current_request_id = None
        self._running = True
        self._accepting = True

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

    def teardown(self):
        """Called once when worker process is stopping.

        Override to clean up resources (close DB connections, flush buffers).
        Called after the run loop exits, before the process terminates.
        Exceptions are logged but do not prevent shutdown.
        """

    def pause(self):
        """Stop accepting new requests from queue.

        Worker continues processing control messages, custom fd
        events, and on_idle(). Call resume() to accept again.
        """
        self._accepting = False

    def resume(self):
        """Resume accepting requests from queue."""
        self._accepting = True

    def keep_alive(self):
        """Signal dispatcher that worker is still processing.

        Call during long operations to prevent request timeout (504)
        and stuck worker detection.
        """
        self._response_queue.put(
            (MSG_HEARTBEAT, self.pool_name,
             self.worker_id, self._current_request_id))

    def on_idle(self):
        """Called on each heartbeat interval when no request arrived.

        Override for periodic background processing.
        """

    def on_disconnect(self, request_id):
        """Called when client disconnects from a deferred/streaming request.

        Override to clean up resources associated with the request.

        Args:
            request_id: ID of the disconnected request.
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
            elif isinstance(msg, tuple) and msg[0] == CTL_DISCONNECT:
                self.on_disconnect(msg[1])

    def do_check(self, request):
        """Validation hook called before routing request to handler.

        Override for authentication, session validation, etc.
        Raise RejectRequest to reject with custom response.

        Args:
            request: Request object with headers, cookies, etc.

        Returns:
            None to continue, or (data, status) tuple to reject.
        """

    def _handle_request(self, request):
        """Route and handle a single request, return Response."""
        try:
            result = self.do_check(request)
            if result is not None:
                data, status = result
                return Response(request.request_id, data=data, status=status)
        except RejectRequest as err:
            return Response(
                request.request_id,
                data=err.data,
                status=err.status)
        except Exception as err:
            self.log.error("do_check: %s\n%s", err, _traceback.format_exc())
            return Response(
                request.request_id,
                data={'error': 'Internal server error'},
                status=500)
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
            if result is DEFERRED:
                return None
            if isinstance(result, Response):
                result.request_id = request.request_id
                return result
            headers = None
            if isinstance(result, tuple):
                if len(result) == 3:
                    data, status, headers = result
                else:
                    data, status = result
            else:
                data, status = result, 200
            return Response(
                request.request_id, data=data,
                status=status, headers=headers)
        except RejectRequest as err:
            return Response(
                request.request_id,
                data=err.data,
                status=err.status)
        except Exception as err:
            return self.on_request_error(request, err)

    def on_request_error(self, request, err):
        """Called when request handler raises an exception.

        Override to customize error handling (e.g., DB reconnect).
        Default logs the error with traceback and returns 500 response.
        """
        self.log.error(
            "%s %s: %s\n%s",
            request.method, request.path, err,
            _traceback.format_exc())
        return Response(
            request.request_id,
            data={'error': str(err)},
            status=500)

    def _apply_memory_limit(self):
        """Cap worker address space via RLIMIT_AS (POSIX only).

        Recognized worker kwarg ``worker_memory_limit_mb``. Runaway
        allocation hits ENOMEM and the worker dies cleanly (the pool
        restarts the slot) instead of exhausting host RAM. No-op where
        the `resource` module is absent (e.g. Windows) or the kwarg is
        unset. setrlimit failures are logged, not fatal.
        """
        limit_mb = self.kwargs.get('worker_memory_limit_mb')
        if not limit_mb or _resource is None:
            return
        nbytes = int(limit_mb) * 1024 * 1024
        try:
            # soft == hard: worker cannot raise it back after a near-crash
            _resource.setrlimit(_resource.RLIMIT_AS, (nbytes, nbytes))
        except (ValueError, OSError) as err:
            self.log.warning("RLIMIT_AS %d MB failed: %s", limit_mb, err)

    def _format_log_name(self):
        """Resolve the logger name from the LOG_NAME template.

        Called in run() (child process) so {pid} is the worker's PID, not
        the dispatcher's. Falls back to a safe default on a bad template.
        """
        ctx = {
            'cls': type(self).__name__,
            'worker_id': self.worker_id,
            'pool_name': self.pool_name,
            'pid': _os.getpid(),
        }
        try:
            return self.LOG_NAME.format(**ctx)
        except (KeyError, IndexError, ValueError) as err:
            self.log.warning(
                "invalid LOG_NAME %r: %s — using default",
                self.LOG_NAME, err)
            return f'{type(self).__name__}[{self.worker_id}]'

    def run(self):
        """Worker main loop using select for multiplexing."""
        _signal.signal(_signal.SIGTERM, lambda *_: None)
        _signal.signal(_signal.SIGINT, lambda *_: None)
        # finalize logger name in the child so {pid} is this process's PID
        self.log.name = self._format_log_name()
        # apply before setup() so the cap also bounds work done there
        self._apply_memory_limit()
        try:
            self._build_routes()
            self.setup()
        except Exception:
            self._response_queue.put(
                (MSG_LOG, self.log.name, LOG_CRITICAL,
                 f"setup() failed:\n{_traceback.format_exc()}"))
            return
        req_reader = self._request_queue._reader
        ctl_reader = self._control_queue._reader
        try:
            self._run_loop(req_reader, ctl_reader)
        finally:
            try:
                self.teardown()
            except Exception:
                self.log.error(
                    "teardown() failed:\n%s", _traceback.format_exc())

    def _run_loop(self, req_reader, ctl_reader):
        while self._running:
            read_fds = [ctl_reader] + list(self._readers)
            if self._accepting:
                read_fds.append(req_reader)
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
            has_custom = False
            for fd in writable:
                if fd in self._writers:
                    self._writers[fd](fd)
                    has_custom = True
            # custom readers
            for fd in readable:
                if fd in self._readers:
                    self._readers[fd](fd)
                    has_custom = True
            # request from dispatcher (skip if custom fd had events)
            if has_custom:
                continue
            if req_reader in readable:
                try:
                    request = self._request_queue.get_nowait()
                except _queue.Empty:
                    continue
                except (EOFError, OSError):
                    break
                request._response_queue = self._response_queue
                self._current_request_id = request.request_id
                self._response_queue.put(
                    (MSG_HEARTBEAT, self.pool_name, self.worker_id, request.request_id))
                response = self._handle_request(request)
                self._current_request_id = None
                if response is not None:
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
            restart_window=300, queue_warning=100,
            recovery_interval=None, **kwargs):
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
            recovery_interval: Seconds after entering degraded state before
                the pool auto-recovers (clears degraded, resets restart
                counter, gives workers a fresh chance). None = sticky
                degraded (default, never auto-recovers).
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
        self._recovery_interval = recovery_interval
        self.kwargs = kwargs
        self.name = worker_class.__name__
        self.request_queue = _mp.Queue()
        self.workers = []
        self._control_queues = []
        self._last_seen = {}
        self._current_request = {}
        self._restart_times = []
        self._degraded = False
        self._degraded_since = None
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
            List of (worker_id, reason, exitcode) tuples for restarted
            workers. exitcode is None for stuck workers (dispatcher killed
            them), otherwise the process exit code (negative = signal:
            -9 OOM, -11 SIGSEGV, -15 SIGTERM, etc.).
        """
        restarted = []
        now = _time.time()
        # clean old restart times
        self._restart_times = [
            t for t in self._restart_times
            if now - t < self.restart_window]
        # auto-recover from degraded after recovery_interval elapses
        if (self._degraded and self._recovery_interval
                and self._degraded_since is not None
                and now - self._degraded_since >= self._recovery_interval):
            # TODO: skip when all slots parked (parking task) — nothing to retry
            self._degraded = False
            self._degraded_since = None
            self._restart_times = []
        for i, worker in enumerate(self.workers):
            reason = None
            exitcode = None
            if not worker.is_alive():
                exitcode = worker.exitcode
                reason = f"died exit={exitcode}"
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
                    if not self._degraded:
                        self._degraded_since = now
                    self._degraded = True
                self._start_worker(i)
                restarted.append((i, reason, exitcode))
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
    def recovery_interval(self):
        return self._recovery_interval

    @property
    def alive_count(self):
        """Number of worker processes currently alive."""
        return sum(1 for w in self.workers if w.is_alive())

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
            'alive_count': self.alive_count,
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
    __slots__ = ('client', 'timestamp', 'pool', 'worker_id', 'streaming')

    def __init__(self, client, pool):
        self.client = client
        self.timestamp = _time.time()
        self.pool = pool
        self.worker_id = None
        self.streaming = False


# Dispatcher

class Dispatcher:
    """Main dispatcher process — HTTP server, routing, worker management.

    Handles static files and @sync routes directly in the main process.
    Routes API requests to worker pools via queues. Uses select()-based
    event loop for multiplexing HTTP sockets, response queue, and custom
    file descriptors.

    Attributes:
        LOG_NAME: Logger name template, formatted with {cls} and {pid}.
            Default '{cls}'; override to customize (e.g. 'gateway-{pid}').
    """

    SELECT_TIMEOUT = 1
    LOG_NAME = '{cls}'

    def __init__(
            self, port=8080, address='0.0.0.0', pools=None,
            static_routes=None, shutdown_timeout=10,
            max_pending=1000, log_level=LOG_INFO, **kwargs):
        """Initialize dispatcher.

        Args:
            port: Listen port.
            address: Listen address.
            pools: List of WorkerPool instances.
            static_routes: Dict of URL prefix -> filesystem path, or
                -> {'path': ..., 'headers': {...}, 'authoritative': bool}.
                'headers' attaches per-mount response headers (e.g.
                Cache-Control). 'authoritative' (default False): when True,
                the mount owns its prefix — a missing file or blocked
                traversal returns 404 instead of falling through to pools.
            shutdown_timeout: Seconds to wait for workers on shutdown.
            max_pending: Max pending requests before rejecting (503).
            ssl_context: Optional ssl.SSLContext for HTTPS.
            **kwargs: Extra arguments passed to HttpServer.
        """
        self._port = port
        self._address = address
        self._pools = pools or []
        self._static_routes = {}
        self._static_headers = {}
        self._static_authoritative = {}
        if static_routes:
            for prefix, value in static_routes.items():
                if isinstance(value, dict):
                    if 'path' not in value:
                        raise ValueError(
                            f"static_routes[{prefix!r}] dict missing 'path'")
                    path = value['path']
                    self._static_headers[prefix] = value.get('headers')
                    self._static_authoritative[prefix] = value.get(
                        'authoritative', False)
                else:
                    path = value
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
        self.log = Logger(
            type(self).__name__,
            sink=self.on_log,
            level=log_level)
        self.log.name = self._format_log_name()
        self._build_sync_routes()

    def _format_log_name(self):
        """Resolve the logger name from the LOG_NAME template.

        Formatted with {cls} and {pid} (the dispatcher process PID).
        Falls back to the class name on a bad template.
        """
        ctx = {'cls': type(self).__name__, 'pid': _os.getpid()}
        try:
            return self.LOG_NAME.format(**ctx)
        except (KeyError, IndexError, ValueError) as err:
            self.log.warning(
                "invalid LOG_NAME %r: %s — using default",
                self.LOG_NAME, err)
            return type(self).__name__

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
        Fires only on a real handler response (PENDING_COMPLETED path);
        use on_pending_removed() for lifecycle cleanup that must run
        regardless of outcome.

        Args:
            response: Response object from worker.
            pending: _PendingRequest with client and pool reference.
        """

    def on_pending_removed(self, request_id, pending, reason):
        """Called exactly once per dispatched request, whatever the outcome.

        Override for side-state cleanup keyed by request_id. Fires after
        the client-facing action (respond/disconnect/control queue put) so
        the dispatcher state is already finalized when this runs.
        Exceptions raised here are logged and swallowed.

        Reason values:
            PENDING_COMPLETED     - handler returned a response, client got it.
                                    on_response() is invoked first.
            PENDING_TIMEOUT       - request exceeded pool.timeout; client got 504.
                                    Worker may still be processing the request.
            PENDING_DISCONNECTED  - client disconnected mid-stream; worker was
                                    notified via control queue (race possible).
            PENDING_STREAM_CLOSED - worker ended the SSE stream cleanly.
            PENDING_SHUTDOWN      - dispatcher is shutting down; client got 503.
            PENDING_WORKER_DIED   - worker process died/was killed while owning
                                    this request; client got 500. on_worker_died()
                                    runs first.

        Args:
            request_id: The request id being removed.
            pending: _PendingRequest snapshot (client, pool, worker_id, ...).
            reason: One of the PENDING_* constants above.
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
        """Try to serve static file. Returns True if served.

        An authoritative mount owns its prefix: a missing file or blocked
        traversal under it returns 404 here (no fall-through to pools).
        """
        path = client.path
        for prefix, base_path in self._static_routes.items():
            if not path.startswith(prefix):
                continue
            authoritative = self._static_authoritative.get(prefix, False)
            rel_path = path[len(prefix):]
            file_path = _os.path.normpath(
                _os.path.join(base_path, rel_path))
            # path traversal protection
            if not (file_path.startswith(base_path + _os.sep)
                    or file_path == base_path):
                if authoritative:
                    return self._serve_static_404(client, file_path)
                continue
            if _os.path.isdir(file_path):
                file_path = _os.path.join(file_path, _DIR_INDEX)
            if _os.path.isfile(file_path):
                cfg_headers = self._static_headers.get(prefix)
                # copy: respond_file/_prepare_response mutate the dict
                # in place (content-length etc.) — must not pollute our
                # stored per-mount config across requests
                client.respond_file(
                    file_path,
                    headers=dict(cfg_headers) if cfg_headers else None)
                self._fire_static_served(client, file_path, 200)
                return True
            if authoritative:
                return self._serve_static_404(client, file_path)
        return False

    def _serve_static_404(self, client, file_path):
        """Serve 404 for an authoritative mount and fire the hook."""
        client.respond({'error': 'Not found'}, status=404)
        self._fire_static_served(client, file_path, 404)
        return True

    def _fire_static_served(self, client, file_path, status):
        """Invoke on_static_served, log and swallow exceptions."""
        try:
            self.on_static_served(client, file_path, status)
        except Exception:
            self.log.error(
                "on_static_served raised:\n%s", _traceback.format_exc())

    def on_static_served(self, client, file_path, status):
        """Called after a static mount serves a response.

        Fires on a 200 (file served) and — for an authoritative mount — on a
        404 (missing file or blocked traversal). Default no-op; override for
        access logs. Exceptions are logged and swallowed.

        Args:
            client: The HTTP connection.
            file_path: Resolved filesystem path that was served or attempted.
            status: 200 or 404.
        """

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
        if pool.alive_count == 0:
            client.respond(
                {'error': 'No workers available'}, status=503,
                headers={'Retry-After': '1'})
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
            content_type=client.content_type,
            remote_address=client.remote_address))

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
            if request_id is not None:
                pending = self._pending.get(request_id)
                if pending is not None:
                    pending.timestamp = _time.time()
                    pending.worker_id = worker_id
        elif msg_type == MSG_LOG:
            _, name, level, message = msg
            self.on_log(name, level, message)
        elif msg_type == MSG_SSE_OPEN:
            _, request_id, content_type, headers, cookies = msg
            pending = self._pending.get(request_id)
            if pending is not None:
                pending.streaming = True
                pending.client.response_stream(
                    content_type=content_type,
                    headers=headers, cookies=cookies)
        elif msg_type == MSG_SSE_EVENT:
            _, request_id, data, event, event_id, retry = msg
            pending = self._pending.get(request_id)
            if pending is not None:
                if event is None and event_id is None and retry is None:
                    ok = pending.client.send_chunk(data)
                else:
                    ok = pending.client.send_event(
                        data=data, event=event,
                        event_id=event_id, retry=retry)
                if not ok:
                    self._stream_disconnected(request_id, pending)
        elif msg_type == MSG_NDJSON:
            _, request_id, obj = msg
            pending = self._pending.get(request_id)
            if pending is not None:
                ok = pending.client.send_ndjson(obj)
                if not ok:
                    self._stream_disconnected(request_id, pending)
        elif msg_type == MSG_SSE_CLOSE:
            _, request_id = msg
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.client.response_stream_end()
                self._notify_pending_removed(
                    request_id, pending, PENDING_STREAM_CLOSED)
        elif msg_type == MSG_RESPONSE:
            _, request_id, response = msg
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.client.respond(
                    response.data,
                    status=response.status,
                    headers=response.headers,
                    cookies=response.cookies)
                self.on_response(response, pending)
                self._notify_pending_removed(
                    request_id, pending, PENDING_COMPLETED)

    def _stream_disconnected(self, request_id, pending):
        """Handle client disconnect during streaming."""
        self._pending.pop(request_id, None)
        if pending.worker_id is not None:
            pool = pending.pool
            if pending.worker_id < len(pool._control_queues):
                pool._control_queues[pending.worker_id].put(
                    (CTL_DISCONNECT, request_id))
        self._notify_pending_removed(
            request_id, pending, PENDING_DISCONNECTED)

    def _notify_pending_removed(self, request_id, pending, reason):
        """Invoke on_pending_removed, log and swallow exceptions."""
        try:
            self.on_pending_removed(request_id, pending, reason)
        except Exception as exc:
            self.on_log(
                pending.pool.name, LOG_ERROR,
                f"on_pending_removed({reason}) raised for "
                f"request {request_id}: {exc!r}")

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
            if not pending.streaming
            and now - pending.timestamp > pending.pool.timeout]
        for request_id in expired:
            pending = self._pending.pop(request_id)
            self.on_log(
                pending.pool.name, LOG_WARNING,
                f"request {request_id} timed out after "
                f"{pending.pool.timeout}s")
            pending.client.respond(
                {'error': 'Request timeout'}, status=504)
            self._notify_pending_removed(
                request_id, pending, PENDING_TIMEOUT)

    def _check_all_workers(self):
        """Check health of all worker pools and queue sizes."""
        for pool in self._pools:
            was_degraded = pool.is_degraded
            restarted = pool.check_workers()
            if pool.is_degraded and not was_degraded:
                self.on_log(pool.name, LOG_WARNING, "entered degraded state")
            elif was_degraded and not pool.is_degraded:
                self.on_log(
                    pool.name, LOG_INFO,
                    "recovered from degraded, retrying workers")
            for worker_id, reason, exitcode in restarted:
                victims = [
                    (rid, p) for rid, p in self._pending.items()
                    if p.pool is pool and p.worker_id == worker_id]
                try:
                    self.on_worker_died(
                        pool, worker_id, reason, exitcode, victims)
                except Exception:
                    self.on_log(
                        pool.name, LOG_ERROR,
                        f"on_worker_died() failed:\n"
                        f"{_traceback.format_exc()}")
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
            t = _time.time()
            ts = _time.strftime(
                '%Y-%m-%d %H:%M:%S', _time.localtime(t)) + \
                f'.{int(t * 1000) % 1000:03d}'
            print(
                f"{color}{ts} {level_name:8s} {name:20s} "
                f"{message}{_ANSI_RESET}",
                file=_sys.stderr)
        else:
            prefix = _LOG_SYSLOG_PREFIX.get(level, '')
            print(f"{prefix}{level_name:8s} {name:20s} {message}",
                file=_sys.stderr)

    def on_worker_died(self, pool, worker_id, reason, exitcode, victims):
        """Called when a worker process died or was killed by the dispatcher.

        Default behavior:
          1. Log restart reason + each victim (request id, client address,
             method, path, body size).
          2. Respond 500 to every victim's client (or response_stream_end()
             for streams), remove them from _pending, and fire
             on_pending_removed(PENDING_WORKER_DIED) for each.

        Override to capture victim payloads (e.g., persist to disk for
        post-mortem) BEFORE calling super(). pending.client gives access
        to method, path, headers, body, address.

        Args:
            pool: WorkerPool the worker belonged to.
            worker_id: Index of the restarted worker.
            reason: 'stuck' or 'died exit=N' (string from check_workers).
            exitcode: Process exit code (int) or None for stuck workers.
                Negative values are signals: -9 OOM, -11 SIGSEGV, etc.
            victims: List of (request_id, _PendingRequest) tuples — requests
                this worker had claimed (via MSG_HEARTBEAT) but never
                completed. May be empty if worker died while idle.
        """
        self.on_log(
            f'{pool.name}[{worker_id}]', LOG_ERROR,
            f"worker restarted: {reason}, "
            f"victims={len(victims)}")
        for request_id, pending in victims:
            c = pending.client
            body_len = len(c.body) if c.body is not None else 0
            self.on_log(
                pool.name, LOG_ERROR,
                f"  victim rid={request_id} from={c.remote_address} "
                f"{c.method} {c.path} body={body_len}B")
            del self._pending[request_id]
            if pending.streaming:
                try:
                    pending.client.response_stream_end()
                except Exception:
                    pass
            else:
                try:
                    pending.client.respond(
                        {'error': 'Worker crashed',
                         'reason': reason},
                        status=500)
                except Exception:
                    pass
            self._notify_pending_removed(
                request_id, pending, PENDING_WORKER_DIED)

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
        # periodic maintenance
        self._check_all_workers()
        if not read_events and not write_events:
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
        for request_id, pending in self._pending.items():
            try:
                pending.client.respond(
                    {'error': 'Server shutting down'}, status=503)
            except Exception:
                pass
            self._notify_pending_removed(
                request_id, pending, PENDING_SHUTDOWN)
        self._pending.clear()
        # shutdown all pools
        for pool in self._pools:
            pool.shutdown(timeout=self._shutdown_timeout)
