"""基于标准库 http.server 的控制台服务。

不引入 Web 框架：整个界面就是一张静态页加五个 JSON 接口，框架会带来新的依赖与体积，
与“缩小镜像”的目标直接冲突。http.server 是线程化的，足够支撑单用户控制台。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from domain_update.auth import AuthGuard
from domain_update.web.api import ApiResponse, WebApi


DEFAULT_PORT = 8501
# 端口是启动期参数，属于部署配置而不是应用配置：它不进配置文件，只走环境变量。
WEB_PORT_ENV = "WEB_PORT"
MIN_PORT = 1
MAX_PORT = 65535


STATIC_DIR = Path(__file__).with_name("static")
# 单页应用没有其他 HTML 入口，路径与文件一一对应；映射不到就是 404，不做首页回落，
# 否则打包错文件名时会静默返回首页，问题难以发现。
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/login.html": "login.html",
    "/login.js": "login.js",
}

LOGIN_PAGE = "/login.html"
# 未登录也能取到的静态资源。必须包含登录页自己的脚本与样式：它们在同一个页面上加载，
# 挡住就会变成「登录页重定向到登录页」，浏览器报 ERR_TOO_MANY_REDIRECTS。
_PUBLIC_STATIC = frozenset({LOGIN_PAGE, "/login.js", "/app.css"})
# 会被浏览器当页面导航请求的两个入口，未登录时用 302 送到登录页。
_PAGE_PATHS = frozenset({"/", "/index.html"})

# 显式列出类型，不依赖 mimetypes：容器基础镜像里通常没有 /etc/mime.types，
# 猜不出来时会把 .js 当成 text/plain，浏览器会拒绝执行。
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

# 页面不加载任何外部资源，因此用最严格的 CSP；'none' 默认值下必须显式放开自己。
# frame-ancestors 与 X-Frame-Options 同时设置：控制台能改 DNS，禁止被任何页面嵌套。
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # 状态里含当时检测到的公网地址，禁止缓存以免展示过期数据。
    "Cache-Control": "no-store",
}

# 请求体上限：配置表单只有十来个短字段，超过这个量级一定是异常请求。
MAX_BODY_BYTES = 64 * 1024

# 本层自己决定、业务层不许覆盖的响应头。重复发同名头会让客户端拿到两个值。
_RESERVED_HEADERS = frozenset({"content-type", "content-length", "connection"})

logger = logging.getLogger(__name__)


def _is_api_path(target: str) -> bool:
    return target == "/healthz" or target.startswith("/api/")


class _Handler(BaseHTTPRequestHandler):
    # 显式声明 HTTP/1.1：默认的 1.0 每个请求都断开连接，页面连续调用接口会反复建连。
    protocol_version = "HTTP/1.1"
    server_version = "DomainUpdateConsole"

    # 覆盖默认实现：http.server 原样走 stderr 且格式与业务日志不一致。
    # 改为统一经 logging 输出到标准输出，`docker logs` 里能和其他日志按时间对齐。
    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 规定的方法名
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 规定的方法名
        self._handle("POST")

    def _handle(self, method: str) -> None:
        target = self.path.split("?", 1)[0]
        if _is_api_path(target):
            # 接口的鉴权闸门在 WebApi.handle() 内部，那里才是读完请求体之后的位置。
            self._handle_api(method)
            return
        if method != "GET":
            # 非 GET 的静态路径本来就不可能成功，先按 405 处理，与加鉴权之前完全一致。
            self._send_text(HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed")
            return
        authorized = self.server.api.is_authorized(dict(self.headers.items()))
        if not authorized and target not in _PUBLIC_STATIC:
            if target in _PAGE_PATHS:
                # 页面导航：送登录页，用户直接就能输密码。
                self._send_redirect(LOGIN_PAGE)
            else:
                # 其余是脚本/未知路径。给脚本请求回一个 HTML 页面会变成「返回 200 的
                # 语法错误」，控制台里很难看懂；直接 401 更准确。顺带也不告诉匿名
                # 调用方哪些文件存在。
                self._send_text(HTTPStatus.UNAUTHORIZED, "Unauthorized")
            return
        if authorized and target == LOGIN_PAGE:
            # 已登录还停在登录页，会让人以为没登进去，直接送回控制台。
            self._send_redirect("/")
            return
        self._send_static(target)

    def _handle_api(self, method: str) -> None:
        raw_body = self._read_body()
        if raw_body is None:
            # 超限时必须关掉连接：请求体还留在套接字里，HTTP/1.1 keep-alive 下这些字节
            # 会被当成下一个请求的请求行解析，日志里出现 "code 414, message
            # Request-URI Too Long"，之后这条连接上的请求全部错乱。
            # 选择关闭而不是读掉，是因为 Content-Length 由客户端给出，读它等于让对端
            # 决定我们在这里分配多少内存。
            self.close_connection = True
            self._send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Payload Too Large")
            return
        # 请求头原样交给 API 层：跨站校验需要 Origin/Host，内容类型校验需要 Content-Type。
        response = self.server.api.handle(
            method,
            self.path,
            raw_body,
            dict(self.headers.items()),
            # 登录失败计数需要来源地址。不读 X-Forwarded-For：那个头由客户端提供，
            # 随手改一下就能绕过限流；反代后面拿到的就是反代地址，属于已知误差。
            client_ip=self.client_address[0] if self.client_address else None,
        )
        self._send_json(response)

    def _read_body(self) -> bytes | None:
        """读取请求体；超出上限时返回 None 并由调用方回复 413。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length)

    def _send_json(self, response: ApiResponse) -> None:
        body = json.dumps(response.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self._send_extra_headers(response.headers)
        self.end_headers()
        self.wfile.write(body)

    def _send_extra_headers(self, headers: Mapping[str, str]) -> None:
        """发出业务层给的额外响应头（Set-Cookie、Retry-After）。

        放在 _send_common_headers() 之后，并挡掉本层已经决定过的名字：
        同名头重复发出去，客户端的取值行为是未定义的。
        """
        reserved = _RESERVED_HEADERS | {name.lower() for name in SECURITY_HEADERS}
        for name, value in headers.items():
            if name.lower() in reserved:
                logger.warning("忽略业务层试图覆盖的响应头：%s", name)
                continue
            self.send_header(name, value)

    def _send_redirect(self, location: str) -> None:
        """302 跳转。

        必须走 _send_common_headers()：少了 Cache-Control: no-store，浏览器可能把这个
        跳转缓存下来，用户登录成功后仍然被送回登录页，而且怎么刷新都好不了。
        """
        body = f"Redirecting to {location}".encode("utf-8")
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, target: str) -> None:
        filename = STATIC_FILES.get(target)
        if filename is None:
            self._send_text(HTTPStatus.NOT_FOUND, "Not Found")
            return
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            # 打包漏文件时给出明确信号，而不是返回空白页让人无从排查。
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "Static asset missing")
            return
        content_type = CONTENT_TYPES.get(
            Path(filename).suffix, "application/octet-stream"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_common_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        # close_connection 可能在处理过程中被置位（请求体超限那条路径），必须显式告知
        # 客户端，否则它会继续在这条已经不再复用的连接上发请求。
        if self.close_connection:
            self.send_header("Connection", "close")


class ConsoleServer(ThreadingHTTPServer):
    # 守护线程：容器收到停止信号时不会被仍在处理的请求拖住。
    daemon_threads = True

    def __init__(self, address: tuple[str, int], api: WebApi) -> None:
        self.api = api
        super().__init__(address, _Handler)


def install_stop_handler(server: ConsoleServer) -> None:
    """把 SIGTERM 转成正常停止。

    容器里本进程就是 PID 1，而 Linux 对 PID 1 会忽略所有未注册处理函数的信号。
    Docker 停止容器时发的 SIGTERM 因此完全无效，只能等满宽限期再被 SIGKILL，
    退出码 137，表现就是 docker stop / compose down 卡很久。显式注册处理函数即可正常退出。
    """

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，正在停止控制台", signum)
        # shutdown() 要等 serve_forever 的循环退出，必须换一个线程调用；
        # 在信号处理函数所在线程里直接调用会死锁。
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _stop)
    except ValueError:
        # 非主线程无法注册信号处理函数（例如测试里从线程启动服务），跳过不影响功能。
        logger.debug("当前不是主线程，跳过 SIGTERM 处理函数注册")


def resolve_web_port(env: Mapping[str, str] | None = None) -> int:
    """解析 WEB_PORT；非法值退回默认端口并记一条警告。

    端口写错不该让容器直接起不来：那时用户连页面都打不开，
    也就看不到任何能帮他定位问题的提示。
    """
    source = os.environ if env is None else env
    raw = (source.get(WEB_PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PORT

    def _fallback(reason: str) -> int:
        logger.warning(
            "%s=%s %s，改用默认端口 %s", WEB_PORT_ENV, raw, reason, DEFAULT_PORT
        )
        return DEFAULT_PORT

    try:
        port = int(raw)
    except ValueError:
        return _fallback("不是整数")
    if not MIN_PORT <= port <= MAX_PORT:
        return _fallback(f"不在 {MIN_PORT}-{MAX_PORT} 范围内")
    return port


def serve(
    host: str = "0.0.0.0", port: int | None = None, auth: AuthGuard | None = None
) -> int:
    """启动控制台并阻塞，直到进程收到停止信号。"""
    if port is None:
        port = resolve_web_port()
    if auth is None:
        # 未显式传入时从环境变量现取。launcher 会先配好日志再构造守卫并传进来，
        # 这条兜底路径服务于直接调用 serve() 的场景，两者共用同一段读取逻辑。
        auth = AuthGuard.from_env()
    server = ConsoleServer((host, port), WebApi(auth=auth))
    install_stop_handler(server)
    logger.info("控制台已启动，监听 %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # 本地直接运行按 Ctrl+C 走这里；容器里收到的是 SIGTERM，走上面的处理函数。
        logger.info("收到中断信号，正在停止控制台")
    finally:
        server.server_close()
    logger.info("控制台已停止")
    return 0
