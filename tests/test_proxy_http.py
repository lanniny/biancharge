import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_http
from proxy_http import configure_socks5_from_dict, parse_socks5_url


class ProxyHttpTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_socks5_from_dict({"enabled": False})

    def test_parse_socks5_url_with_auth(self) -> None:
        host, port, user, password = parse_socks5_url(
            "socks5://127.0.0.1:9498"
        )
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 9498)
        self.assertIsNone(user)
        self.assertIsNone(password)

    def test_configure_socks5_reads_dotenv_when_service_env_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv = Path(temp_dir) / ".env"
            dotenv.write_text("BINANCE_SOCKS5_PROXY=socks5://127.0.0.1:9498\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
                proxy_http, "DOTENV_PATH", str(dotenv)
            ):
                info = configure_socks5_from_dict({"enabled": True, "url": "env:BINANCE_SOCKS5_PROXY"})

        self.assertEqual(info, {"enabled": True, "host": "127.0.0.1", "port": 9498})

    def test_dotenv_disable_overrides_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv = Path(temp_dir) / ".env"
            dotenv.write_text(
                "\n".join(
                    [
                        "AUTOTRADER_SOCKS5_DISABLED=1",
                        "BINANCE_SOCKS5_PROXY=socks5://127.0.0.1:9498",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
                proxy_http, "DOTENV_PATH", str(dotenv)
            ):
                info = configure_socks5_from_dict({"enabled": True, "url": "env:BINANCE_SOCKS5_PROXY"})

        self.assertEqual(info, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
