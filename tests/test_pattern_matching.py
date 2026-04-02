"""Tests for URL pattern matching and parameter parsing."""

import unittest

from uhttp.workers import _parse_param, _match_pattern, _match_prefix


class TestParseParam(unittest.TestCase):

    def test_not_a_param(self):
        self.assertIsNone(_parse_param('users'))

    def test_simple_param(self):
        name, converter = _parse_param('{id}')
        self.assertEqual(name, 'id')
        self.assertIs(converter, str)

    def test_int_param(self):
        name, converter = _parse_param('{id:int}')
        self.assertEqual(name, 'id')
        self.assertIs(converter, int)

    def test_float_param(self):
        name, converter = _parse_param('{price:float}')
        self.assertEqual(name, 'price')
        self.assertIs(converter, float)

    def test_str_param(self):
        name, converter = _parse_param('{name:str}')
        self.assertEqual(name, 'name')
        self.assertIs(converter, str)

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            _parse_param('{id:uuid}')

    def test_empty_braces(self):
        name, converter = _parse_param('{}')
        self.assertEqual(name, '')
        self.assertIs(converter, str)

    def test_not_braces(self):
        self.assertIsNone(_parse_param('{incomplete'))
        self.assertIsNone(_parse_param('incomplete}'))


class TestMatchPattern(unittest.TestCase):

    def test_exact_match(self):
        result = _match_pattern('/users', '/users')
        self.assertEqual(result, {})

    def test_exact_no_match(self):
        result = _match_pattern('/users', '/items')
        self.assertIsNone(result)

    def test_different_length(self):
        result = _match_pattern('/users', '/users/123')
        self.assertIsNone(result)

    def test_str_param(self):
        result = _match_pattern('/user/{name}', '/user/john')
        self.assertEqual(result, {'name': 'john'})

    def test_int_param(self):
        result = _match_pattern('/user/{id:int}', '/user/42')
        self.assertEqual(result, {'id': 42})

    def test_int_param_invalid(self):
        result = _match_pattern('/user/{id:int}', '/user/abc')
        self.assertIsNone(result)

    def test_float_param(self):
        result = _match_pattern('/price/{amount:float}', '/price/9.99')
        self.assertEqual(result, {'amount': 9.99})

    def test_float_param_invalid(self):
        result = _match_pattern('/price/{amount:float}', '/price/abc')
        self.assertIsNone(result)

    def test_multiple_params(self):
        result = _match_pattern(
            '/org/{org}/user/{id:int}', '/org/acme/user/42')
        self.assertEqual(result, {'org': 'acme', 'id': 42})

    def test_mixed_static_and_param(self):
        result = _match_pattern(
            '/api/user/{id:int}/profile', '/api/user/42/profile')
        self.assertEqual(result, {'id': 42})

    def test_mixed_no_match(self):
        result = _match_pattern(
            '/api/user/{id:int}/profile', '/api/user/42/settings')
        self.assertIsNone(result)

    def test_root_path(self):
        result = _match_pattern('/', '/')
        self.assertEqual(result, {})

    def test_trailing_slash_pattern(self):
        result = _match_pattern('/users/', '/users/')
        self.assertEqual(result, {})

    def test_int_zero(self):
        result = _match_pattern('/item/{id:int}', '/item/0')
        self.assertEqual(result, {'id': 0})

    def test_int_negative(self):
        result = _match_pattern('/item/{id:int}', '/item/-1')
        self.assertEqual(result, {'id': -1})

    def test_float_int_value(self):
        result = _match_pattern('/val/{v:float}', '/val/10')
        self.assertEqual(result, {'v': 10.0})


class TestMatchPrefix(unittest.TestCase):

    def test_glob_match(self):
        self.assertTrue(_match_prefix('/api/users/**', '/api/users/123'))

    def test_glob_match_deep(self):
        self.assertTrue(
            _match_prefix('/api/users/**', '/api/users/123/profile'))

    def test_glob_match_root(self):
        self.assertTrue(_match_prefix('/api/**', '/api/anything'))

    def test_glob_no_match(self):
        self.assertFalse(_match_prefix('/api/users/**', '/api/items/123'))

    def test_glob_no_match_partial(self):
        self.assertFalse(_match_prefix('/api/users/**', '/api/usersx/123'))

    def test_exact_match(self):
        self.assertTrue(_match_prefix('/health', '/health'))

    def test_exact_no_match(self):
        self.assertFalse(_match_prefix('/health', '/version'))

    def test_exact_no_match_longer(self):
        self.assertFalse(_match_prefix('/health', '/health/check'))

    def test_glob_prefix_only(self):
        # /api/users itself should match /api/users/**
        self.assertTrue(_match_prefix('/api/users/**', '/api/users'))

    def test_glob_single_segment(self):
        self.assertTrue(_match_prefix('/api/**', '/api/x'))


if __name__ == '__main__':
    unittest.main()
