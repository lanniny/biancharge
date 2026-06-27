import os

os.environ.setdefault("AUTOTRADER_SOCKS5_DISABLED", "1")

from proxy_http import configure_socks5_proxy

configure_socks5_proxy(enabled=False)
