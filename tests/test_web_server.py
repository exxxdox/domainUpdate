"""HTTP 服务层的连接行为测试。

这里必须起真实套接字：请求体超限时如何处置留在连接里的字节，取决于 HTTP/1.1
keep-alive 的复帧规则，在 WebApi.handle() 那一层观测不到。
"""

import http.client
import threading
from collections.abc import Iterator

import pytest

from domain_update.web.api import WebApi
from domain_update.web.server import MAX_BODY_BYTES, ConsoleServer


@pytest.fixture()
def server() -> Iterator[ConsoleServer]:
    """在随机空闲端口上起一个真实服务，测试结束再关掉。"""
    instance = ConsoleServer(("127.0.0.1", 0), WebApi())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _connect(server: ConsoleServer) -> http.client.HTTPConnection:
    # server_address 在 typeshed 里是宽松的联合类型，这里已固定绑定 IPv4。
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    return http.client.HTTPConnection(host, port, timeout=10)


def test_keep_alive_connection_serves_multiple_requests(server: ConsoleServer) -> None:
    """正常请求必须保持连接复用，否则下面的关闭断言就失去了区分度。"""
    connection = _connect(server)
    try:
        for _ in range(2):
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            assert response.status == 200
    finally:
        connection.close()


def test_oversized_body_is_rejected_and_connection_closed(server: ConsoleServer) -> None:
    """请求体超限时回 413 并关闭连接。

    只回 413 而不处置留在套接字里的请求体，keep-alive 下这些字节会被当成下一个请求的
    请求行解析，日志里出现 "code 414, message Request-URI Too Long"，之后的请求全部错乱。
    选择关闭而不是读掉，是因为 Content-Length 由客户端给出，读它等于让对端决定我们
    在这里分配多少内存。
    """
    connection = _connect(server)
    try:
        connection.request(
            "POST",
            "/api/ipv6",
            body=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 413
        assert response.getheader("Connection") == "close"
        assert response.will_close is True
    finally:
        connection.close()
