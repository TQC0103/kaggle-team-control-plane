import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from agent_interface.http_client import ApiClient, ApiError


class FakeResponse:
    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, _size):
        return self.body


class ApiClientTests(unittest.TestCase):
    @patch("agent_interface.http_client.urlopen")
    def test_request_encodes_query_body_and_auth(self, mocked_open):
        mocked_open.return_value = FakeResponse()
        client = ApiClient("http://127.0.0.1:8765/", "secret", timeout=4)

        result = client.request(
            "POST", "/api/test", query={"enabled": False}, body={"hello": "world"}
        )

        request = mocked_open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/test?enabled=false")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(json.loads(request.data), {"hello": "world"})
        self.assertEqual(result, {"ok": True})

    @patch("agent_interface.http_client.urlopen")
    def test_http_error_is_structured(self, mocked_open):
        mocked_open.side_effect = HTTPError(
            "http://test", 409, "Conflict", {}, FakeResponse(b'{"error":"account busy"}')
        )
        with self.assertRaises(ApiError) as raised:
            ApiClient("http://test").request("GET", "/api/accounts")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.message, "account busy")

    @patch("agent_interface.http_client.urlopen")
    def test_nested_backend_error_message_is_preserved(self, mocked_open):
        mocked_open.side_effect = HTTPError(
            "http://test",
            422,
            "Unprocessable Entity",
            {},
            FakeResponse(b'{"error":{"code":"validation_error","message":"name is required"}}'),
        )
        with self.assertRaises(ApiError) as raised:
            ApiClient("http://test").request("POST", "/api/batches", body={})
        self.assertEqual(raised.exception.message, "name is required")

    @patch("agent_interface.http_client.urlopen")
    def test_connection_error_does_not_expose_token(self, mocked_open):
        mocked_open.side_effect = URLError("connection refused")
        with self.assertRaises(ApiError) as raised:
            ApiClient("http://test", "do-not-leak").request("GET", "/api/accounts")
        self.assertNotIn("do-not-leak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
