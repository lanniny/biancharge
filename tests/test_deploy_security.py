import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploySecurityTests(unittest.TestCase):
    def test_socks_proxy_url_uses_environment_reference(self) -> None:
        for rel in ("market_autotrader.vps.json", "market_autotrader.growth.example.json"):
            path = ROOT / rel
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            proxy = data.get("execution", {}).get("socks5_proxy", {})
            if proxy.get("enabled"):
                self.assertEqual(proxy.get("url"), "env:BINANCE_SOCKS5_PROXY")

    def test_no_hardcoded_socks_credentials_in_source_config_or_tests(self) -> None:
        candidates = [
            "proxy_http.py",
            "market_autotrader.vps.json",
            "market_autotrader.growth.example.json",
            "tests/test_proxy_http.py",
        ]
        credential_url = re.compile(r"socks5h?://[^/\s:@]+:[^@\s]+@")
        for rel in candidates:
            path = ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(credential_url.search(text), rel)

    def test_systemd_service_pins_vps_config_and_loop_delay(self) -> None:
        text = (ROOT / "deploy/market-autotrader.service").read_text(encoding="utf-8")
        self.assertIn("Environment=CONFIG=market_autotrader.vps.json", text)
        self.assertIn("Environment=LOOP_DELAY=90", text)
        self.assertIn("EnvironmentFile=-/home/deploy/market-autotrader/.env", text)

    def test_runner_default_uses_vps_config_not_example_live_config(self) -> None:
        text = (ROOT / "deploy/run_autotrader.sh").read_text(encoding="utf-8")
        self.assertIn('CONFIG="${CONFIG:-market_autotrader.vps.json}"', text)
        self.assertNotIn("market_autotrader.growth.example.json", text)

    def test_gitignore_excludes_runtime_secrets_and_local_state(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        required = [
            ".env",
            ".env.*",
            "logs/",
            "approvals/",
            ".venv/",
            ".claude/",
            ".ai-shared/",
            "market_autotrader.vps.json",
            "market_autotrader.open-now.json",
            "market_autotrader.close-spcx.json",
            "config/pinned-symbols.json",
        ]
        for pattern in required:
            self.assertIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
