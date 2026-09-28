"""HTTP 服务层的连接行为测试。

这里必须起真实套接字：请求体超限时如何处置留在连接里的字节，取决于 HTTP/1.1
keep-alive 的复帧规则，在 WebApi.handle() 那一层观测不到。
"""

import http.client
import json
import threading
import urllib.request
from collections.abc import Iterator

import pytest

from domain_update.auth import COOKIE_NAME, AuthGuard, Credentials
from domain_update.web.api import WebApi
from domain_update.web.server import MAX_BODY_BYTES, ConsoleServer

AUTH_USERNAME = "admin"
AUTH_PASSWORD = "s3cret-password"


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
        response = _request_oversized(connection)

        # None = 客户端拿到连接重置，同样是「已拒绝并断开」，见 _request_oversized 的说明。
        if response is not None:
            assert response.status == 413
            assert response.getheader("Connection") == "close"
            assert response.will_close is True
    finally:
        connection.close()


# ---- 登录闸门（只能在连接层观测） -------------------------------------------


@pytest.fixture()
def auth_server() -> Iterator[ConsoleServer]:
    """启用鉴权的服务。响应头（Set-Cookie / Location）只有走真实套接字才看得到。"""
    guard = AuthGuard(Credentials(username=AUTH_USERNAME, password=AUTH_PASSWORD))
    instance = ConsoleServer(("127.0.0.1", 0), WebApi(auth=guard))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _request(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    response.read()
    return response


def _request_oversized(
    connection: http.client.HTTPConnection, path: str = "/api/ipv6"
) -> http.client.HTTPResponse | None:
    """发送超限请求体并取响应；返回 None 表示客户端拿到的是连接重置。

    服务端拒绝超限请求体时会「回 413 后立刻关闭」，而**不读掉**套接字里剩下的字节
    （读它等于让对端决定我们分配多少内存，见 server._read_body 的注释）。如果关闭发生在
    客户端还在发剩余字节的时候，对端拿到的就是 RST：已到达的 413 可能被丢弃，read 直接抛错。
    这是该设计的固有竞态，两种结果都是「已拒绝」，测试必须两者都接受，否则会随机失败——
    而随机失败的测试比没有测试更糟。

    异常类型按平台各不相同，实际观测到的是 Windows 的 ConnectionAbortedError
    （WinError 10053），Linux 上多为 ConnectionResetError，套接字被直接丢弃时则是
    RemoteDisconnected。三者都归入「已拒绝」。
    """
    try:
        return _request(
            connection,
            "POST",
            path,
            body=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
    except (
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        http.client.RemoteDisconnected,
    ):
        return None


def _login(connection: http.client.HTTPConnection) -> str:
    """登录并返回可直接放进 Cookie 头的会话值。"""
    body = json.dumps({"username": AUTH_USERNAME, "password": AUTH_PASSWORD}).encode()
    response = _request(
        connection,
        "POST",
        "/api/login",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 200
    # 形如 du_session=<token>; Path=/; HttpOnly; ...
    return response.getheader("Set-Cookie").split(";", 1)[0]


def test_unauthenticated_api_returns_401_json(auth_server: ConsoleServer) -> None:
    connection = _connect(auth_server)
    try:
        response = _request(connection, "GET", "/api/state")

        assert response.status == 401
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert response.getheader("Set-Cookie") is None
    finally:
        connection.close()


def test_unauthenticated_root_redirects_to_login(auth_server: ConsoleServer) -> None:
    connection = _connect(auth_server)
    try:
        response = _request(connection, "GET", "/")

        assert response.status == 302
        assert response.getheader("Location") == "/login.html"
        # 302 也必须带 no-store：被缓存的话登录成功后仍会被送回登录页。
        assert response.getheader("Cache-Control") == "no-store"
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/login.html", "/login.js", "/app.css"])
def test_login_page_assets_are_public(auth_server: ConsoleServer, path: str) -> None:
    """登录页自己的资源必须可匿名获取，否则就是无限重定向（ERR_TOO_MANY_REDIRECTS）。"""
    connection = _connect(auth_server)
    try:
        response = _request(connection, "GET", path)

        assert response.status == 200
        assert response.getheader("Location") is None
    finally:
        connection.close()


def test_unauthenticated_app_js_is_401_without_redirect(auth_server: ConsoleServer) -> None:
    """脚本请求不能重定向到 HTML：浏览器会报解析错误，噪音大且没有意义。"""
    connection = _connect(auth_server)
    try:
        response = _request(connection, "GET", "/app.js")

        assert response.status == 401
        assert response.getheader("Location") is None
    finally:
        connection.close()


def test_login_then_reuse_cookie_reaches_the_console(auth_server: ConsoleServer) -> None:
    connection = _connect(auth_server)
    try:
        cookie = _login(connection)
        response = _request(connection, "GET", "/", headers={"Cookie": cookie})

        assert response.status == 200
        assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    finally:
        connection.close()


def test_login_cookie_carries_security_attributes(auth_server: ConsoleServer) -> None:
    connection = _connect(auth_server)
    try:
        body = json.dumps({"username": AUTH_USERNAME, "password": AUTH_PASSWORD}).encode()
        response = _request(
            connection,
            "POST",
            "/api/login",
            body=body,
            headers={"Content-Type": "application/json"},
        )

        cookie = response.getheader("Set-Cookie")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Max-Age=" in cookie
    finally:
        connection.close()


def test_logged_in_visit_to_login_page_redirects_home(auth_server: ConsoleServer) -> None:
    connection = _connect(auth_server)
    try:
        cookie = _login(connection)
        response = _request(connection, "GET", "/login.html", headers={"Cookie": cookie})

        assert response.status == 302
        assert response.getheader("Location") == "/"
    finally:
        connection.close()


def test_healthz_is_reachable_without_a_session(auth_server: ConsoleServer) -> None:
    """复刻 Dockerfile 里 HEALTHCHECK 的调用方式：它跟随重定向，所以这里不能是 3xx。

    若把 /healthz 也挡掉，容器会永远 unhealthy，而 Docker 不会因为 unhealthy 重启容器，
    故障是静默的。
    """
    host = str(auth_server.server_address[0])
    port = int(auth_server.server_address[1])

    with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
        assert response.status == 200
        # 走的是统一信封，状态在 data 里。
        assert json.loads(response.read())["data"]["status"] == "ok"


def test_unauthenticated_oversized_body_is_still_413(auth_server: ConsoleServer) -> None:
    """闸门必须在读完请求体之后才生效。

    否则未登录的超大请求体会留在套接字里，keep-alive 下被当成下一个请求的请求行解析。
    鉴权不该改变这条路径的结果：仍然是 413 + 关闭连接，绝不是 401。
    """
    connection = _connect(auth_server)
    try:
        response = _request_oversized(connection)

        if response is not None:
            assert response.status == 413
            assert response.getheader("Connection") == "close"
    finally:
        connection.close()


def test_unauthenticated_post_leaves_the_connection_usable(
    auth_server: ConsoleServer,
) -> None:
    """被拒的 POST 必须把请求体读干净，否则同一条连接上的下一个请求会错乱。"""
    connection = _connect(auth_server)
    try:
        body = json.dumps({"provider": "cloudflare"}).encode()
        rejected = _request(
            connection,
            "POST",
            "/api/ipv6",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        assert rejected.status == 401

        follow_up = _request(connection, "GET", "/healthz")

        assert follow_up.status == 200
    finally:
        connection.close()


def test_unauthenticated_post_to_static_path_is_405(auth_server: ConsoleServer) -> None:
    """静态路径上的非 GET 本来就不可能成功，先按 405 处理，与加鉴权前一致。"""
    connection = _connect(auth_server)
    try:
        assert _request(connection, "POST", "/index.html").status == 405
    finally:
        connection.close()


def test_head_is_not_implemented(auth_server: ConsoleServer) -> None:
    """HEAD 不在支持范围内。这条用例是防止有人「顺手」加上 do_HEAD 打开新面。

    http.server 对未实现的方法回 501，请求根本到不了 _handle，因此不存在绕过闸门的路径。
    """
    connection = _connect(auth_server)
    try:
        assert _request(connection, "HEAD", "/").status == 501
    finally:
        connection.close()
