#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from env_loader import get_proxy, load_dotenv, request_proxy_modes, ssl_verify


class EnvLoaderTests(unittest.TestCase):
    def test_load_dotenv_strips_quotes_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                'export FINNHUB_API_KEY="abc123"\n'
                "KIMI_API_KEY='kimi-key'\n"
                "# comment\n"
                "EMPTY_SKIP\n"
                "PLAIN=unquoted\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in ("FINNHUB_API_KEY", "KIMI_API_KEY", "PLAIN"):
                    os.environ.pop(key, None)
                load_dotenv(str(env_path))
                self.assertEqual(os.environ["FINNHUB_API_KEY"], "abc123")
                self.assertEqual(os.environ["KIMI_API_KEY"], "kimi-key")
                self.assertEqual(os.environ["PLAIN"], "unquoted")

    def test_load_dotenv_does_not_override_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FINNHUB_API_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FINNHUB_API_KEY": "from-env"}):
                load_dotenv(str(env_path))
                self.assertEqual(os.environ["FINNHUB_API_KEY"], "from-env")

    def test_get_proxy_empty_when_unset(self):
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "", "https_proxy": "", "HTTP_PROXY": "", "http_proxy": ""},
            clear=False,
        ):
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("https_proxy", None)
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("http_proxy", None)
            self.assertEqual(get_proxy(), "")

    def test_request_proxy_modes_direct_only_without_proxy(self):
        self.assertEqual(request_proxy_modes(""), [(None, "direct")])

    def test_request_proxy_modes_proxy_then_direct(self):
        modes = request_proxy_modes("http://127.0.0.1:1082")
        self.assertEqual(modes[0][1], "proxy")
        self.assertEqual(modes[0][0]["https"], "http://127.0.0.1:1082")
        self.assertEqual(modes[1], (None, "direct"))

    def test_ssl_verify_defaults_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IB_RESEARCH_SSL_VERIFY", None)
            self.assertTrue(ssl_verify())
        with mock.patch.dict(os.environ, {"IB_RESEARCH_SSL_VERIFY": "0"}):
            self.assertFalse(ssl_verify())


if __name__ == "__main__":
    unittest.main()
