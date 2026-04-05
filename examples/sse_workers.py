"""SSE (Server-Sent Events) example with deferred responses.

Demonstrates:
- SSE streaming from worker to client
- Deferred responses (DEFERRED sentinel)
- Client disconnect handling (on_disconnect)
- keep_alive() for long operations

Test with:
    curl -N http://localhost:8080/events
    curl -N http://localhost:8080/events?channel=news
    curl -X POST http://localhost:8080/publish -d '{"msg": "hello"}'
    curl -X POST http://localhost:8080/publish -d '{"channel": "news", "msg": "breaking"}'
"""

import uhttp.workers as _workers


class EventWorker(_workers.Worker):
    """Worker that manages SSE subscribers and publishes events."""

    def setup(self):
        self._subscribers = {}  # request_id → (request, channel)

    @_workers.api('/events', 'GET')
    def subscribe(self, request):
        channel = (request.query or {}).get('channel', 'default')
        request.response_stream()
        request.send_event(
            data={'status': 'connected', 'channel': channel},
            event='open')
        self._subscribers[request.request_id] = (request, channel)
        if len(self._subscribers) >= self.kwargs.get('max_subscribers', 100):
            self.pause()
        return _workers.DEFERRED

    @_workers.api('/publish', 'POST')
    def publish(self, request):
        channel = (request.data or {}).get('channel', 'default')
        msg = (request.data or {}).get('msg', '')
        count = 0
        for req, ch in self._subscribers.values():
            if ch == channel:
                req.send_event(data={'msg': msg}, event='message')
                count += 1
        return {'published': count, 'channel': channel}

    def on_disconnect(self, request_id):
        sub = self._subscribers.pop(request_id, None)
        if sub:
            self.log.info("Client disconnected: %d", request_id)
            max_subs = self.kwargs.get('max_subscribers', 100)
            if not self._accepting and len(self._subscribers) < max_subs:
                self.resume()

    def on_idle(self):
        # periodic keepalive to all subscribers
        for req, _ in list(self._subscribers.values()):
            req.send_event(event='ping')


class MyDispatcher(_workers.Dispatcher):
    @_workers.sync('/health')
    def health(self, client, path_params):
        client.respond({'status': 'ok'})


def main():
    pool = _workers.WorkerPool(
        EventWorker, num_workers=2,
        log_level=_workers.LOG_INFO,
        timeout=30)
    dispatcher = MyDispatcher(
        port=8080,
        pools=[pool])
    dispatcher.run()


if __name__ == '__main__':
    main()
