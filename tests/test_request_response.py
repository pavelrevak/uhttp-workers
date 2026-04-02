"""Tests for Request and Response objects."""

import unittest
import pickle

from uhttp.workers import Request, Response


class TestRequest(unittest.TestCase):

    def test_basic_creation(self):
        req = Request(1, 'GET', '/users')
        self.assertEqual(req.request_id, 1)
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.path, '/users')
        self.assertIsNone(req.query)
        self.assertIsNone(req.data)
        self.assertEqual(req.headers, {})
        self.assertIsNone(req.content_type)
        self.assertEqual(req.path_params, {})

    def test_full_creation(self):
        req = Request(
            42, 'POST', '/upload',
            query={'page': '1'},
            data=b'\x89PNG',
            headers={'content-type': 'image/png'},
            content_type='image/png')
        self.assertEqual(req.request_id, 42)
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, '/upload')
        self.assertEqual(req.query, {'page': '1'})
        self.assertEqual(req.data, b'\x89PNG')
        self.assertEqual(req.headers, {'content-type': 'image/png'})
        self.assertEqual(req.content_type, 'image/png')

    def test_path_params_mutable(self):
        req = Request(1, 'GET', '/user/42')
        req.path_params = {'id': 42}
        self.assertEqual(req.path_params, {'id': 42})

    def test_pickle_json_data(self):
        req = Request(1, 'POST', '/data', data={'key': 'value'})
        pickled = pickle.dumps(req)
        restored = pickle.loads(pickled)
        self.assertEqual(restored.request_id, 1)
        self.assertEqual(restored.data, {'key': 'value'})

    def test_pickle_binary_data(self):
        data = b'\x00\x01\x02\xff' * 1000
        req = Request(1, 'POST', '/upload', data=data)
        pickled = pickle.dumps(req)
        restored = pickle.loads(pickled)
        self.assertEqual(restored.data, data)

    def test_pickle_large_binary(self):
        data = bytes(range(256)) * 4096  # ~1MB
        req = Request(1, 'POST', '/upload', data=data)
        pickled = pickle.dumps(req)
        restored = pickle.loads(pickled)
        self.assertEqual(len(restored.data), len(data))
        self.assertEqual(restored.data, data)

    def test_pickle_with_headers(self):
        headers = {
            'content-type': 'application/json',
            'x-api-key': 'secret',
            'x-custom': 'value',
        }
        req = Request(1, 'GET', '/', headers=headers)
        pickled = pickle.dumps(req)
        restored = pickle.loads(pickled)
        self.assertEqual(restored.headers, headers)


class TestResponse(unittest.TestCase):

    def test_default(self):
        resp = Response(1)
        self.assertEqual(resp.request_id, 1)
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.data)
        self.assertIsNone(resp.headers)

    def test_with_data(self):
        resp = Response(1, data={'result': 'ok'}, status=201)
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.data, {'result': 'ok'})

    def test_error_response(self):
        resp = Response(1, data={'error': 'not found'}, status=404)
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.data['error'], 'not found')

    def test_with_headers(self):
        resp = Response(
            1, data='ok',
            headers={'X-Custom': 'value'})
        self.assertEqual(resp.headers, {'X-Custom': 'value'})

    def test_binary_response(self):
        data = b'\x89PNG\r\n\x1a\n'
        resp = Response(1, data=data)
        self.assertEqual(resp.data, data)

    def test_pickle(self):
        resp = Response(
            42, data={'items': [1, 2, 3]},
            status=200, headers={'X-Total': '3'})
        pickled = pickle.dumps(resp)
        restored = pickle.loads(pickled)
        self.assertEqual(restored.request_id, 42)
        self.assertEqual(restored.data, {'items': [1, 2, 3]})
        self.assertEqual(restored.status, 200)
        self.assertEqual(restored.headers, {'X-Total': '3'})

    def test_pickle_binary_response(self):
        data = bytes(range(256)) * 100
        resp = Response(1, data=data)
        pickled = pickle.dumps(resp)
        restored = pickle.loads(pickled)
        self.assertEqual(restored.data, data)


if __name__ == '__main__':
    unittest.main()
