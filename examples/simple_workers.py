"""Simple API server example with multiple worker pools and handlers."""

import time

import uhttp.workers as _workers


VALID_API_KEYS = {'demo-key-1', 'demo-key-2'}


# Handlers — grouped by URL prefix

class ComputeHandler(_workers.ApiHandler):
    PATTERN = '/api/compute'

    @_workers.api('/factorial/{n:int}', 'GET')
    def factorial(self, request):
        n = request.path_params['n']
        if n < 0 or n > 1000:
            return {'error': 'n must be between 0 and 1000'}, 400
        result = 1
        for i in range(2, n + 1):
            result *= i
        return {'n': n, 'factorial': str(result)}

    @_workers.api('/echo', 'POST')
    def echo(self, request):
        """Echo back posted data after simulated processing."""
        time.sleep(0.1)  # simulate work
        return {'echo': request.data}


class ItemsHandler(_workers.ApiHandler):
    PATTERN = '/api/items'

    @_workers.api('', 'GET')
    def list_items(self, request):
        return {'items': list(self.worker.items.values())}

    @_workers.api('', 'POST')
    def create_item(self, request):
        item_id = self.worker.next_id
        self.worker.next_id += 1
        item = {'id': item_id, 'name': request.data.get('name', '')}
        self.worker.items[item_id] = item
        return item, 201


class ItemHandler(_workers.ApiHandler):
    PATTERN = '/api/item'

    @_workers.api('/{id:int}', 'GET')
    def get_item(self, request):
        item = self.worker.items.get(request.path_params['id'])
        if not item:
            return {'error': 'Not found'}, 404
        return item

    @_workers.api('/{id:int}', 'DELETE')
    def delete_item(self, request):
        item_id = request.path_params['id']
        if item_id not in self.worker.items:
            return {'error': 'Not found'}, 404
        del self.worker.items[item_id]
        return {'deleted': item_id}


# Workers

class ComputeWorker(_workers.Worker):
    """CPU-intensive worker."""
    HANDLERS = [ComputeHandler]


class StorageWorker(_workers.Worker):
    """Database/storage worker — CRUD operations."""
    HANDLERS = [ItemsHandler, ItemHandler]

    def setup(self):
        # in real app: self.db = Database(self.kwargs['db_login'])
        self.items = {}
        self.next_id = 1


# Dispatcher with API key validation and sync handlers

class MyDispatcher(_workers.Dispatcher):
    def do_check(self, client):
        api_key = client.headers.get('x-api-key')
        if api_key not in VALID_API_KEYS:
            client.respond({'error': 'unauthorized'}, status=401)
            raise _workers.RejectRequest()

    def on_response(self, response, pending):
        """Forward successful compute results to storage pool."""
        if response.status == 200 and pending.pool.name == 'ComputeWorker':
            storage_pool = self._find_pool('/api/items')
            if storage_pool:
                storage_pool.request_queue.put(_workers.Request(
                    request_id=-1,
                    method='POST',
                    path='/api/items',
                    data={'name': f"result: {response.data}"}))

    def on_idle(self):
        """Periodic dispatcher maintenance."""
        pass

    @_workers.sync('/')
    def index(self, client, path_params):
        client.respond_redirect('/static/index.html')

    @_workers.sync('/health')
    def health(self, client, path_params):
        client.respond({
            'status': 'ok',
            'pools': [pool.status() for pool in self._pools],
        })

    @_workers.sync('/version')
    def version(self, client, path_params):
        client.respond({'version': '1.0.0'})


STATIC_ROUTES = {
    '/static/': './static/',
}


def main():
    compute_worker = _workers.WorkerPool(
        ComputeWorker, num_workers=4,
        routes=['/api/compute/**'],
        timeout=60)
    storage_worker = _workers.WorkerPool(
        StorageWorker, num_workers=2,
        routes=['/api/items/**', '/api/item/**'],
        timeout=10)
    dispatcher = MyDispatcher(
        port=8080,
        pools=[compute_worker, storage_worker],
        static_routes=STATIC_ROUTES)
    dispatcher.run()


if __name__ == '__main__':
    main()
