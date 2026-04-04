# uhttp-workers

Multi-process worker dispatcher built on [uhttp-server](https://github.com/cortexm/uhttp-server).

Single dispatcher process handles all connections, N worker processes handle business logic in parallel. Communication via `multiprocessing.Queue` with efficient `select()` integration (POSIX only).

## Architecture

```
                                    ┌─────────────┐
                                    │  Worker 0   │
                                    │  Worker 1   │
                 request_queue A    │  Worker 2   │
              ┌────────────────►    │  Worker 3   │  ComputeWorker
              │                     └──────┬──────┘
              │                            │
┌─────────────┤                            │
│             │                     ┌──────┴──────┐
│ Dispatcher  │  request_queue B    │  Worker 0   │
│  (main)     ├────────────────►    │  Worker 1   │  StorageWorker
│             │                     └──────┬──────┘
│  - static   │                            │
│  - sync     │    response_queue          │
│  - auth     │◄───────────────────────────┘
│             │    (shared)
└─────────────┘
```

**Key design decisions:**

- Sockets never leave the dispatcher — only serializable data goes through queues
- Each worker pool has its own request queue, all pools share one response queue
- Workers send heartbeats via response queue — dispatcher detects stuck/dead workers
- Each worker has a private control queue for config updates and stop signals

## Installation

```bash
pip install uhttp-workers
```

## Quick Start

```python
import uhttp.workers as _workers


class ItemWorker(_workers.Worker):
    def setup(self):
        self.items = {}
        self.next_id = 1

    @_workers.api('/api/items', 'GET')
    def list_items(self, request):
        return {'items': list(self.items.values())}

    @_workers.api('/api/item/{id:int}', 'GET')
    def get_item(self, request):
        item = self.items.get(request.path_params['id'])
        if not item:
            return {'error': 'Not found'}, 404
        return item


class MyDispatcher(_workers.Dispatcher):
    def do_check(self, client):
        api_key = client.headers.get('x-api-key')
        if api_key not in VALID_KEYS:
            client.respond({'error': 'unauthorized'}, status=401)
            raise _workers.RejectRequest()

    @_workers.sync('/health')
    def health(self, client, path_params):
        client.respond({'status': 'ok'})


def main():
    dispatcher = MyDispatcher(
        port=8080,
        pools=[
            _workers.WorkerPool(
                ItemWorker, num_workers=4,
                routes=['/api/**'],
                timeout=30,
            ),
        ],
    )
    dispatcher.run()


if __name__ == '__main__':
    main()
```

## Multiple Worker Pools

Route different endpoints to different worker pools with independent scaling:

```python
dispatcher = MyDispatcher(
    port=8080,
    pools=[
        _workers.WorkerPool(
            ComputeWorker, num_workers=4,
            routes=['/api/compute/**'],
            timeout=60,
        ),
        _workers.WorkerPool(
            StorageWorker, num_workers=2,
            routes=['/api/items/**', '/api/item/**'],
            timeout=10,
        ),
        _workers.WorkerPool(
            GeneralWorker, num_workers=1,
        ),  # no routes = default/fallback pool
    ],
)
```

Request routing order:
1. **Static files** — served directly by dispatcher
2. **Sync handlers** — run in dispatcher process
3. **Worker pools** — first pool with matching route prefix, or fallback pool
4. **404** — no match

## API Handlers

Group related endpoints under a common URL prefix using `ApiHandler`:

```python
import uhttp.workers as _workers

class UserHandler(_workers.ApiHandler):
    PATTERN = '/api/user'

    @_workers.api('', 'GET')
    def list_users(self, request):
        return {'users': self.worker.db.list_users()}

    @_workers.api('/{id:int}', 'GET')
    def get_user(self, request):
        return self.worker.db.get_user(request.path_params['id'])

    @_workers.api('/{id:int}', 'DELETE')
    def delete_user(self, request):
        self.worker.db.delete_user(request.path_params['id'])
        return {'deleted': request.path_params['id']}

class OrderHandler(_workers.ApiHandler):
    PATTERN = '/api/order'

    @_workers.api('', 'GET')
    def list_orders(self, request):
        return {'orders': []}

    @_workers.api('/{id:int}', 'GET')
    def get_order(self, request):
        return {'id': request.path_params['id']}

class MyWorker(_workers.Worker):
    HANDLERS = [UserHandler, OrderHandler]

    def setup(self):
        self.db = Database(self.kwargs['db_login'])
```

`@api` patterns on handlers are automatically prefixed with the handler's `PATTERN`. Handlers access the worker instance via `self.worker`.

You can also define `@api` methods directly on the worker class — useful for simple workers that don't need handler grouping.

Handlers support inheritance — a subclass inherits all routes from its parent, using the subclass `PATTERN` as prefix.

## Static Files

```python
dispatcher = Dispatcher(
    port=8080,
    static_routes={
        '/static/': './static/',
        '/images/': '/var/data/images/',
    },
)
```

Static files are served directly by the dispatcher process with path traversal protection. Directory requests automatically serve `index.html` if present.

## Sync Handlers

Lightweight handlers that run directly in the dispatcher process — no queue overhead. Define them as methods on the dispatcher class with the `@sync` decorator:

```python
import uhttp.workers as _workers

class MyDispatcher(_workers.Dispatcher):
    @_workers.sync('/health')
    def health(self, client, path_params):
        client.respond({
            'status': 'ok',
            'pools': [pool.status() for pool in self._pools],
        })

    @_workers.sync('/version')
    def version(self, client, path_params):
        client.respond({'version': '1.0.0'})
```

Use sync handlers for fast, non-blocking responses only — long operations block the entire dispatcher.

## Worker Lifecycle

### Setup

`setup()` is called once when a worker process starts. Use it to initialize resources that cannot be shared across processes (database connections, models, etc.):

```python
class MyWorker(_workers.Worker):
    def setup(self):
        self.db = Database(self.kwargs['db_login'])
```

Extra keyword arguments from `WorkerPool(...)` are available as `self.kwargs`.

### Configuration Updates

Dispatcher can send configuration to workers at runtime via per-worker control queues:

```python
# dispatcher side
for pool in dispatcher._pools:
    pool.send_config({'rate_limit': 100})

# worker side
class MyWorker(_workers.Worker):
    def on_config(self, config):
        self.rate_limit = config['rate_limit']
```

### Health Monitoring

Workers send heartbeats automatically via the shared response queue. When a worker takes a request, it reports which `request_id` it is working on. If a worker stops responding:

- **Dead worker** (segfault, crash) — detected via `is_alive()`, restarted immediately
- **Stuck worker** (infinite loop in C code) — detected via heartbeat timeout, killed and restarted
- **Too many restarts** — pool marked as degraded, returns 503

### Request Handling

```python
@_workers.api('/process/{id:int}', 'POST')
def process(self, request):
    # request.request_id  — internal ID for dispatcher pairing
    # request.method       — 'POST'
    # request.path         — '/process/42'
    # request.path_params  — {'id': 42}
    # request.query        — {'page': '1'} or None
    # request.data         — dict (JSON), bytes (binary), or None
    # request.headers      — dict
    # request.cookies      — dict (lazy-parsed from Cookie header)
    # request.content_type — 'application/json'

    # return data (status 200)
    return {'result': 'ok'}

    # return data with status
    return {'error': 'not found'}, 404

    # defer response — worker continues accepting requests
    return _workers.DEFERRED
```

### Deferred Responses

Return `DEFERRED` to skip immediate response. The worker stays in the select loop, accepts new requests, and sends the response later via `request.respond()`:

```python
class MyWorker(_workers.Worker):
    def setup(self):
        self._jobs = {}

    @_workers.api('/process', 'POST')
    def process(self, request):
        job_id = start_background_work(request.data)
        self._jobs[job_id] = request
        return _workers.DEFERRED

    def on_work_done(self, job_id, result):
        request = self._jobs.pop(job_id)
        request.respond(data={'result': result})
```

Note: deferred requests are still subject to dispatcher timeout — call `self.keep_alive()` periodically to prevent 504.

### Keep Alive

Call `self.keep_alive()` during long operations to reset both the request timeout and stuck worker detection:

```python
@_workers.api('/export', 'POST')
def export(self, request):
    for chunk in generate_large_export():
        self.keep_alive()
    return {'status': 'done'}
```

## URL Patterns

Dispatcher uses prefix matching to route requests to pools:

```python
_workers.WorkerPool(MyWorker, routes=['/api/users/**'])  # matches /api/users/anything
_workers.WorkerPool(MyWorker, routes=['/api/status'])     # exact match only
_workers.WorkerPool(MyWorker)                              # fallback — catches everything else
```

Workers use full pattern matching with type conversion:

```python
@_workers.api('/user/{id:int}', 'GET')        # id converted to int
@_workers.api('/price/{amount:float}', 'GET') # amount converted to float
@_workers.api('/tag/{name}')                   # name as str, all methods
```

## Authentication

Override `do_check()` on the dispatcher — runs before any request is queued:

```python
class MyDispatcher(_workers.Dispatcher):
    def __init__(self, valid_keys, **kwargs):
        super().__init__(**kwargs)
        self.valid_keys = valid_keys

    def do_check(self, client):
        api_key = client.headers.get('x-api-key')
        if api_key not in self.valid_keys:
            client.respond({'error': 'unauthorized'}, status=401)
            raise _workers.RejectRequest()
```

`do_check()` is only called for requests going to worker pools — static files and sync handlers are not affected.

### Worker-Level Validation

Override `do_check()` on the worker — runs before routing to handler:

```python
class MyWorker(_workers.Worker):
    def do_check(self, request):
        token = request.cookies.get('session')
        if not token:
            return {'error': 'unauthorized'}, 401
```

Return `(data, status)` tuple to reject, or `None` to continue. You can also raise `RejectRequest`:

```python
    def do_check(self, request):
        if not self.validate_token(request.cookies.get('session')):
            raise _workers.RejectRequest(
                data={'error': 'forbidden'}, status=403)
```

`RejectRequest` accepts optional `data` (default: `{'error': 'Rejected'}`) and `status` (default: `403`).

### Error Handling

Override `on_request_error()` on the worker to customize error handling when a request handler raises an exception:

```python
class MyWorker(_workers.Worker):
    def on_request_error(self, request, err):
        if isinstance(err, DatabaseError):
            self.db.reconnect()
        return super().on_request_error(request, err)
```

Default behavior logs the error with traceback and returns 500.

## Post-Response Hook

Override `on_response()` on the dispatcher to post-process after a response is sent to the client — e.g., forward data to another worker pool:

```python
class MyDispatcher(_workers.Dispatcher):
    def on_response(self, response, pending):
        if response.status == 200 and pending.pool.name == 'LprWorker':
            storage_pool = self._find_pool('/internal/storage')
            storage_pool.request_queue.put(_workers.Request(
                request_id=-1,
                method='POST',
                path='/internal/storage/save',
                data={
                    'image': pending.client.data,
                    'result': response.data,
                }))
```

`pending` is a `_PendingRequest` with `client` (original connection) and `pool` (source pool).
Requests with `request_id=-1` are ignored by the dispatcher when the worker responds.

## Dispatcher Idle Hook

Override `on_idle()` on the dispatcher for periodic background tasks — called on each `select()` timeout (every `SELECT_TIMEOUT` seconds, default 1s):

```python
class MyDispatcher(_workers.Dispatcher):
    def on_idle(self):
        # periodic cleanup, monitoring, etc.
        pass
```

Workers have their own `on_idle()` hook, called on each `heartbeat_interval` timeout.

## Graceful Shutdown

On `SIGTERM` or `SIGINT`:

1. Stop accepting new connections
2. Wait for pending responses (up to `shutdown_timeout`)
3. Respond 503 to remaining pending requests
4. Send stop signal to all workers via control queues
5. Wait for workers to finish, kill after timeout

## Monitoring

```python
class MyDispatcher(_workers.Dispatcher):
    @_workers.sync('/monitor')
    def monitor(self, client, path_params):
        client.respond({
            'pools': [pool.status() for pool in self._pools],
            'pending': len(self._pending),
        })
```

Pool status includes per-worker info: alive, last seen, current request ID, queue size.

## Logging

Workers have a built-in `Logger` accessible via `self.log`:

```python
class MyWorker(_workers.Worker):
    @_workers.api('/item/{id:int}', 'GET')
    def get_item(self, request):
        item_id = request.path_params['id']
        # %-style (Python logging compatible)
        self.log.info("Getting item %d", item_id)
        # {}-style (kwargs)
        self.log.info("Getting item {id}", id=item_id)
        return {'id': item_id}
```

Log messages are sent to the dispatcher via the shared response queue and printed in the dispatcher process — no interleaved output from multiple processes.

**Log levels:** `LOG_DEBUG` (10), `LOG_INFO` (20), `LOG_WARNING` (30), `LOG_ERROR` (40), `LOG_CRITICAL` (50)

Check current level with `is_*` properties to skip expensive formatting:

```python
if self.log.is_debug:
    self.log.debug("Details: %s", expensive_computation())
```

Available: `is_debug`, `is_info`, `is_warning`, `is_error`.

Set minimum level per pool:

```python
_workers.WorkerPool(
    MyWorker, num_workers=4,
    log_level=_workers.LOG_INFO,  # default: LOG_WARNING
)
```

**Output format** — auto-detected at dispatcher init:
- **Terminal:** ANSI colors — bold red (critical), red (error), yellow (warning), dim (debug)
- **systemd:** Syslog priority prefixes (`<3>`, `<4>`, etc.) — journalctl colors by priority

Override `Dispatcher.on_log(name, level, message)` to customize output or forward to a logging framework.

Errors are logged automatically:
- Handler exceptions → ERROR with full traceback (returns 500 to client)
- `setup()` crash → CRITICAL with traceback (worker exits and restarts)
- Worker restart → ERROR with reason (died/stuck)
- Request timeout → WARNING with request ID and timeout value

## Configuration

### Dispatcher

| Parameter | Default | Description |
|-----------|---------|-------------|
| `port` | 8080 | Listen port |
| `address` | `'0.0.0.0'` | Listen address |
| `pools` | `[]` | List of `WorkerPool` instances |
| `static_routes` | `{}` | URL prefix → filesystem path |
| `shutdown_timeout` | 10 | Seconds to wait on shutdown |
| `max_pending` | 1000 | Max pending requests (503 when exceeded) |
| `ssl_context` | `None` | `ssl.SSLContext` for HTTPS |

### WorkerPool

| Parameter | Default | Description |
|-----------|---------|-------------|
| `worker_class` | — | `Worker` subclass |
| `num_workers` | 1 | Number of worker processes |
| `routes` | `None` | Prefix patterns (`None` = fallback pool) |
| `timeout` | 30 | Request timeout in seconds (504) |
| `stuck_timeout` | 60 | Heartbeat timeout before kill |
| `heartbeat_interval` | 1 | Seconds between worker heartbeats |
| `log_level` | `LOG_WARNING` | Minimum log level for worker loggers |
| `max_restarts` | 10 | Max restarts per `restart_window` |
| `restart_window` | 300 | Time window for restart counting (seconds) |
| `queue_warning` | 100 | Log warning when queue size exceeds this (0 = disable) |

Extra `**kwargs` on `WorkerPool` are passed to worker constructor (accessible as `self.kwargs`).

## Requirements

- Python >= 3.10
- POSIX system (Linux, macOS) — uses `select()` with `queue._reader`
- [uhttp-server](https://github.com/cortexm/uhttp-server)
