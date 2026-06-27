from proxy_http import parse_socks5_url


def test_parse_socks5_url_with_auth() -> None:
    host, port, user, password = parse_socks5_url(
        "socks5://127.0.0.1:9498"
    )
    assert host == "127.0.0.1"
    assert port == 9498
    assert user is None
    assert password is None
