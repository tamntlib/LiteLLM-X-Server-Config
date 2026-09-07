import io
import unittest
import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

from llmproxy.core.http import DEFAULT_HTTP_TIMEOUT, format_http_error, request_json


class HttpSafetyTest(unittest.TestCase):
    def test_request_json_uses_bounded_timeout_by_default(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            request_json("http://example.test")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], DEFAULT_HTTP_TIMEOUT)

    def test_http_error_body_is_not_logged_by_default(self):
        error = urllib.error.HTTPError(
            "http://example.test",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(b'{"api_key":"super-secret"}'),
        )
        formatted = format_http_error(error)
        self.assertIn("HTTP Error 400", formatted)
        self.assertNotIn("super-secret", formatted)
        self.assertNotIn("Body:", formatted)


if __name__ == "__main__":
    unittest.main()
