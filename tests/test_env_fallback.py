import tempfile
import unittest
from pathlib import Path
from unittest import mock

import market_autotrader as ma
from market_autotrader import ExecutionConfig, expand_env, resolve_api_credentials


class EnvFallbackTests(unittest.TestCase):
    def test_expand_env_reads_dotenv_when_process_env_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv = Path(temp_dir) / ".env"
            dotenv.write_text("BINANCE_API_KEY=dotenv-key\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
                ma, "DOTENV_PATH", str(dotenv)
            ):
                self.assertEqual(expand_env("env:BINANCE_API_KEY"), "dotenv-key")

    def test_resolve_api_credentials_reads_dotenv_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv = Path(temp_dir) / ".env"
            dotenv.write_text(
                "BINANCE_API_KEY=dotenv-key\nBINANCE_API_SECRET='dotenv-secret'\n",
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
                ma, "DOTENV_PATH", str(dotenv)
            ):
                self.assertEqual(
                    resolve_api_credentials(ExecutionConfig()),
                    ("dotenv-key", "dotenv-secret"),
                )


if __name__ == "__main__":
    unittest.main()
