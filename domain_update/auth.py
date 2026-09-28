"""登录鉴权：凭据来自环境变量，会话是无状态签名 Cookie。

设计取舍（与项目既有约束一致）：

- 零新增依赖。口令派生与签名用标准库 hmac/hashlib，Cookie 解析用 http.cookies。
- 凭据属于部署参数，和 WEB_PORT 一样只走环境变量。应用配置（服务商密钥等）只能来自
  页面写入的 config.json，「同一个字段不允许有两个来源」。
- 会话不落服务端状态：Cookie 里带用户名与过期时间，用 HMAC 签名。密钥由凭据派生，
  因此改密码/改用户名会让所有已签发的 Cookie 立刻失效——这就是无状态方案里的全局登出。
- 凭据未配置时返回一个 disabled 的守卫并记 WARNING，而不是拒绝启动：升级既有部署
  不该因为少两个环境变量就起不来（此时用户连页面都打不开，看不到任何提示）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

WEB_USERNAME_ENV = "WEB_USERNAME"
WEB_PASSWORD_ENV = "WEB_PASSWORD"

COOKIE_NAME = "du_session"
SESSION_TTL_SECONDS = 12 * 60 * 60

# 派生密钥用的固定盐。目的不是抗离线爆破（口令本身就是明文环境变量），
# 而是把「口令变了」稳定映射成「密钥变了」，从而一次性作废所有会话。
_KEY_SALT = b"domain-update-session"
_KEY_ITERATIONS = 200_000

THROTTLE_MAX_FAILURES = 5
THROTTLE_WINDOW_SECONDS = 15 * 60
# 计数表硬上限，防止大量不同来源把内存和单次写入耗时推高（见 record_failure）。
MAX_TRACKED_CLIENTS = 1024

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


def missing_credential_variables(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """返回没有配置（或只有空白）的凭据变量名，0 到 2 个。

    单独抽出来是为了让启动入口能区分两种性质完全不同的情况：
    两个都缺 = 明确不启用登录（可接受的部署方式）；**恰好缺一个 = 配置写错了**。
    后者如果也按「未配置」放行，结果就是以为开了登录、实际控制台裸奔。
    """
    source = os.environ if env is None else env
    # 密码不做 strip：口令可能真的以空格开头或结尾，改了就是改了用户的口令，
    # 因此这里只判断它是不是「全空白」。
    values = (
        (WEB_USERNAME_ENV, (source.get(WEB_USERNAME_ENV) or "").strip()),
        (WEB_PASSWORD_ENV, (source.get(WEB_PASSWORD_ENV) or "").strip()),
    )
    return tuple(name for name, value in values if not value)


def load_credentials(env: Mapping[str, str] | None = None) -> Credentials | None:
    """读取凭据；两个变量都非空才算配置好，否则返回 None（不鉴权）。

    返回 None 只是「本模块不做判断」，**调用方仍需区分缺一个还是缺两个**：
    见 `missing_credential_variables`，launcher 用它把「只配一半」变成启动失败。

    `env` 可注入，便于测试，与 web/server.py 的 resolve_web_port 保持同一种写法。
    """
    source = os.environ if env is None else env
    missing = missing_credential_variables(source)
    if missing:
        # 点名缺的是哪一个：只配一半时最可能的原因就是变量名写错了。
        logger.warning(
            "未配置 %s，控制台不校验登录，任何能访问该端口的人都可以修改 DNS",
            " 与 ".join(missing),
        )
        return None
    return Credentials(
        username=(source.get(WEB_USERNAME_ENV) or "").strip(),
        password=source.get(WEB_PASSWORD_ENV) or "",
    )


def _derive_key(credentials: Credentials) -> bytes:
    # 用户名也参与派生：换账号名同样能让旧 Cookie 失效，不需要额外的失效表。
    material = f"{credentials.username}\x00{credentials.password}".encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", material, _KEY_SALT, _KEY_ITERATIONS)


def _b64encode(raw: bytes) -> str:
    # 去掉 '=' 填充：Cookie 值里带上等号要转义，去掉后解码时补回来即可。
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes | None:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError):
        return None


def _session_token(cookie_header: str | None) -> str | None:
    """从 Cookie 请求头里取会话值；畸形头返回 None（按未登录处理）。

    不区分「没有 Cookie」和「Cookie 坏了」：给攻击者的反馈越少越好。
    SimpleCookie 能正确切分多个 Cookie，但遇到畸形内容会抛 CookieError。
    """
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    if morsel is None or not morsel.value:
        return None
    return morsel.value


class AuthGuard:
    """无状态会话的签发与校验。凭据未配置时用 disabled() 得到一个恒放行的实例。"""

    def __init__(self, credentials: Credentials | None = None) -> None:
        self._credentials = credentials
        # 派生很贵（20 万次迭代），只在构造时做一次，绝不能放进请求路径。
        self._key = b"" if credentials is None else _derive_key(credentials)

    @classmethod
    def disabled(cls) -> AuthGuard:
        return cls(None)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthGuard:
        return cls(load_credentials(env))

    @property
    def enabled(self) -> bool:
        return self._credentials is not None

    # ---- 会话 ----------------------------------------------------------------

    def _sign(self, body: str) -> str:
        # 用 utf-8 而不是 ascii：body 来自请求里的 Cookie，可能是任意字节序列，
        # ascii 编码遇到非 ASCII 会抛 UnicodeEncodeError，直接把这个请求变成 500。
        digest = hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).digest()
        return _b64encode(digest)

    def issue(self, now: float | None = None) -> str:
        """签发会话令牌。凭据未配置时不签发（调用方应先判断 enabled）。"""
        if self._credentials is None:
            raise RuntimeError("未配置凭据时不应签发会话")
        issued_at = time.time() if now is None else now
        payload = {
            "u": self._credentials.username,
            "exp": int(issued_at) + SESSION_TTL_SECONDS,
        }
        body = _b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str | None, now: float | None = None) -> bool:
        """校验令牌。任何一步失败都返回 False，不区分原因。"""
        if self._credentials is None or not token:
            return False
        body, separator, signature = token.partition(".")
        if not separator or not body or not signature:
            return False
        # 先比签名再解析内容：签名不对时不必让 JSON 解析器去碰对方给的字节。
        # 比较用 bytes 而不是 str：URL 里取到的可能是非 ASCII，compare_digest 对
        # 非 ASCII 的 str 会抛 TypeError，那会直接变成 500。
        if not hmac.compare_digest(self._sign(body).encode("ascii"), signature.encode("utf-8")):
            return False
        raw = _b64decode(body)
        if raw is None:
            return False
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("u") != self._credentials.username:
            return False
        expires_at = payload.get("exp")
        # bool 是 int 的子类，True 会被当成过期时间 1（1970 年），这里显式排掉。
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return False
        current = time.time() if now is None else now
        return expires_at > current

    def authenticate(self, username: object, password: object) -> bool:
        """比对登录表单提交的用户名与口令。

        两个比较都要做且都用固定时间比较：提前 return 会让响应耗时泄露「用户名对不对」。
        入参来自 JSON 请求体，可能是任意类型，所以先转 str 再比，编码失败判否。
        """
        if self._credentials is None:
            return False
        try:
            submitted_user = str(username).encode("utf-8")
            submitted_password = str(password).encode("utf-8")
        except UnicodeEncodeError:
            # 单独的代理项编不出 UTF-8，判否而不是抛异常。
            return False
        user_ok = hmac.compare_digest(
            submitted_user, self._credentials.username.encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            submitted_password, self._credentials.password.encode("utf-8")
        )
        return user_ok and password_ok

    # ---- HTTP 层用到的判定与 Cookie -------------------------------------------

    def is_authorized(self, headers: Mapping[str, str]) -> bool:
        """凭据未配置时恒放行；否则要求会话 Cookie 有效。"""
        if self._credentials is None:
            return True
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        return self.verify(_session_token(normalized.get("cookie")))

    def login_cookie(self, token: str) -> str:
        """登录成功时下发的 Set-Cookie 值。

        不加 Secure：部署是明文 HTTP（通常在带反向代理的后面），加上它浏览器在直连场景
        根本不会回送 Cookie，表现为「密码对却一直跳回登录页」。

        用 SameSite=Lax 而不是 Strict：本控制台所有写操作都是 POST，Lax 已经不会在跨站
        POST 与子资源请求上带上 Cookie，CSRF 面已经封住；而 Strict 会让「从聊天软件或书签
        点进来」这种顶层跳转不带 Cookie，用户每次访问都要重新登录一次。
        """
        return (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )

    def logout_cookie(self) -> str:
        """退出登录：下发同名空 Cookie 并立即过期。

        服务端没有会话表可清，登出只是让浏览器丢掉令牌；真正让令牌失效的手段是改口令。
        """
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


class LoginThrottle:
    """按客户端地址限制登录失败次数，挡在线爆破。

    进程内计数，容器重启即清零。它不替代口令强度：反向代理后面所有请求的来源地址
    都是代理本身，此时计数会退化成全局计数，因此只算纵深防御的一层。
    """

    def __init__(
        self,
        max_failures: int = THROTTLE_MAX_FAILURES,
        window_seconds: float = THROTTLE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._failures)

    def is_blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client)) >= self._max_failures

    def retry_after_seconds(self, client: str) -> int:
        """还要等多少秒才恢复；没有限流时返回 0。

        供 429 响应带 Retry-After：只说「稍后再试」会让调用方盲目重试。
        """
        with self._lock:
            stamps = self._recent(client)
            if len(stamps) < self._max_failures:
                return 0
            # 最早那次失败滑出窗口时，计数才会掉回阈值以下。
            elapsed = self._clock() - stamps[len(stamps) - self._max_failures]
            return max(1, math.ceil(self._window - elapsed))

    def record_failure(self, client: str) -> None:
        with self._lock:
            # 每次写入做一遍全量清理：条目数上限就是窗口内出现过的来源地址数，
            # 长期运行不会无限堆积（失败登录本身是低频事件，这点开销可忽略）。
            self._purge_expired()
            stamps = self._recent(client)
            stamps.append(self._clock())
            self._failures[client] = stamps
            # 清理只能删掉「已过期」的条目。用大量不同来源（IPv6 /64、僵尸网络）打过来时，
            # 窗口内条目数仍然会线性增长，而每次写入都要扫全表，退化成 O(n²)。
            # 因此再加一道硬上限：超了按最久未失败的先淘汰，宁可少记几个来源。
            while len(self._failures) > MAX_TRACKED_CLIENTS:
                oldest = min(self._failures, key=lambda key: self._failures[key][-1])
                del self._failures[oldest]

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    # 下面两个只在持锁时调用。
    def _recent(self, client: str) -> list[float]:
        now = self._clock()
        stamps = [stamp for stamp in self._failures.get(client, []) if now - stamp < self._window]
        if stamps:
            self._failures[client] = stamps
        else:
            self._failures.pop(client, None)
        return stamps

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            client
            for client, stamps in self._failures.items()
            if not stamps or now - stamps[-1] >= self._window
        ]
        for client in expired:
            del self._failures[client]
