"""登录鉴权的纯逻辑测试。

这一层不碰网络也不碰文件：凭据来自环境变量、会话是无状态签名 Cookie，
所有判断都是「给定输入 → 布尔值」，可以完全在内存里验证。
真实 HTTP 行为（302 / Set-Cookie）在 tests/test_web_server.py 里测。
"""

import json
from base64 import urlsafe_b64encode

import pytest

from domain_update.auth import (
    COOKIE_NAME,
    MAX_TRACKED_CLIENTS,
    SESSION_TTL_SECONDS,
    AuthGuard,
    Credentials,
    LoginThrottle,
    load_credentials,
    missing_credential_variables,
)

USERNAME = "admin"
PASSWORD = "correct horse battery staple"


def _guard(username: str = USERNAME, password: str = PASSWORD) -> AuthGuard:
    return AuthGuard(Credentials(username=username, password=password))


# ---- 凭据读取 ---------------------------------------------------------------


def test_load_credentials_returns_none_when_both_missing() -> None:
    assert load_credentials({}) is None


@pytest.mark.parametrize(
    "env",
    [
        {"WEB_USERNAME": USERNAME},
        {"WEB_PASSWORD": PASSWORD},
        {"WEB_USERNAME": USERNAME, "WEB_PASSWORD": ""},
        {"WEB_USERNAME": "   ", "WEB_PASSWORD": PASSWORD},
    ],
    ids=["缺少密码", "缺少用户名", "密码为空串", "用户名只有空白"],
)
def test_load_credentials_returns_none_when_incomplete(env: dict[str, str]) -> None:
    """半配置必须整体视为未配置。

    只看其中一个变量会产生「有用户名没密码」这种谁也登不进去的状态，
    比明确不鉴权更难排查。
    """
    assert load_credentials(env) is None


def test_load_credentials_reads_both_values() -> None:
    credentials = load_credentials({"WEB_USERNAME": USERNAME, "WEB_PASSWORD": PASSWORD})

    assert credentials == Credentials(username=USERNAME, password=PASSWORD)


def test_missing_credential_variables_distinguishes_half_from_none() -> None:
    """launcher 靠这个区分「明确不启用登录」与「配置写错了」，两者后果完全不同。"""
    assert missing_credential_variables({}) == ("WEB_USERNAME", "WEB_PASSWORD")
    assert missing_credential_variables({"WEB_USERNAME": USERNAME}) == ("WEB_PASSWORD",)
    assert missing_credential_variables({"WEB_PASSWORD": PASSWORD}) == ("WEB_USERNAME",)
    assert (
        missing_credential_variables(
            {"WEB_USERNAME": "  ", "WEB_PASSWORD": PASSWORD}
        )
        == ("WEB_USERNAME",)
    )
    assert (
        missing_credential_variables(
            {"WEB_USERNAME": USERNAME, "WEB_PASSWORD": PASSWORD}
        )
        == ()
    )


def test_load_credentials_trims_username_but_keeps_password_verbatim() -> None:
    """.env 里拖一个空格是常见笔误；密码则可能真的以空格结尾，不能改。"""
    credentials = load_credentials(
        {"WEB_USERNAME": f"  {USERNAME}  ", "WEB_PASSWORD": f" {PASSWORD} "}
    )

    assert credentials == Credentials(username=USERNAME, password=f" {PASSWORD} ")


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"WEB_PASSWORD": PASSWORD}, "WEB_USERNAME"),
        ({"WEB_USERNAME": USERNAME}, "WEB_PASSWORD"),
    ],
    ids=["缺用户名", "缺密码"],
)
def test_load_credentials_warning_names_the_missing_variable(
    env: dict[str, str], missing: str, caplog: pytest.LogCaptureFixture
) -> None:
    """只配一半通常是变量名写错了，日志必须点名是哪一个，否则无从排查。"""
    with caplog.at_level("WARNING"):
        assert load_credentials(env) is None

    assert any(missing in record.getMessage() for record in caplog.records)
    assert any("不校验登录" in record.getMessage() for record in caplog.records)


def test_load_credentials_does_not_warn_when_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        load_credentials({"WEB_USERNAME": USERNAME, "WEB_PASSWORD": PASSWORD})

    assert caplog.records == []


# ---- 开关状态 ---------------------------------------------------------------


def test_disabled_guard_authorizes_every_request() -> None:
    """未配置凭据时按既定取舍放行，兼容既有部署。"""
    assert AuthGuard.disabled().is_authorized({}) is True


def test_guard_reports_whether_it_is_enabled() -> None:
    assert AuthGuard.disabled().enabled is False
    assert _guard().enabled is True


def test_disabled_guard_rejects_authentication() -> None:
    """没有凭据时不存在「登录成功」这回事，接口层据此回 400。"""
    assert AuthGuard.disabled().authenticate(USERNAME, PASSWORD) is False


# ---- 签发与校验 -------------------------------------------------------------


def test_issued_token_verifies() -> None:
    guard = _guard()

    assert guard.verify(guard.issue()) is True


def test_token_is_rejected_after_password_change() -> None:
    """密钥由凭据派生，改密码即让所有已发 Cookie 失效——无状态会话的全局登出。"""
    token = _guard().issue()

    assert _guard(password="a different password").verify(token) is False


def test_token_signed_for_another_user_is_rejected() -> None:
    """密钥里混入用户名，别的账号签出的 token 在本账号上无效。"""
    guard = _guard()
    other = AuthGuard(Credentials(username="someone-else", password=PASSWORD))

    assert guard.verify(other.issue()) is False


def test_expired_token_is_rejected() -> None:
    guard = _guard()
    issued_at = 1_000_000.0

    token = guard.issue(now=issued_at)

    assert guard.verify(token, now=issued_at + SESSION_TTL_SECONDS - 1) is True
    # 边界：到期时刻本身已经失效，避免出现「刚好卡在零点还能用」的歧义。
    assert guard.verify(token, now=issued_at + SESSION_TTL_SECONDS) is False


def test_tampered_signature_is_rejected() -> None:
    guard = _guard()
    body, _, signature = guard.issue().partition(".")

    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

    assert guard.verify(f"{body}.{flipped}") is False


def test_tampered_payload_is_rejected() -> None:
    """把用户名换成别人、或把过期时间推到未来，签名都会对不上。"""
    guard = _guard()
    _, _, signature = guard.issue().partition(".")
    forged = (
        urlsafe_b64encode(json.dumps({"u": "root", "exp": 2**31}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )

    assert guard.verify(f"{forged}.{signature}") is False


@pytest.mark.parametrize(
    "token",
    [None, "", "no-dot", ".", "body.", ".signature", "a.b.c"],
    ids=["None", "空串", "没有分隔符", "只有点", "缺签名", "缺载荷", "多一个点"],
)
def test_malformed_tokens_are_rejected(token: str | None) -> None:
    assert _guard().verify(token) is False


@pytest.mark.parametrize(
    "token",
    ["载荷.签名", "????.????", "\u0000.\u0000", "ünïcödé.signature"],
    ids=["非 ASCII", "非 base64 字符", "控制字符", "带变音符号"],
)
def test_verify_never_raises_on_non_ascii_token(token: str) -> None:
    """Cookie 值可以是任意字节序列。抛异常会让请求在 server 层直接掉连接（不是 4xx）。"""
    assert _guard().verify(token) is False


# ---- Cookie 解析 ------------------------------------------------------------


def test_is_authorized_requires_the_session_cookie() -> None:
    assert _guard().is_authorized({}) is False
    assert _guard().is_authorized({"cookie": "other=1"}) is False


def test_is_authorized_accepts_token_among_other_cookies() -> None:
    guard = _guard()
    header = f"theme=dark; {COOKIE_NAME}={guard.issue()}; lang=zh"

    assert guard.is_authorized({"cookie": header}) is True


def test_is_authorized_ignores_header_name_case() -> None:
    """服务端把原始请求头交给这一层，大小写不保证统一。"""
    guard = _guard()

    assert guard.is_authorized({"Cookie": f"{COOKIE_NAME}={guard.issue()}"}) is True


@pytest.mark.parametrize(
    "header",
    ["=broken", f"{COOKIE_NAME}=a b c", ";;;", COOKIE_NAME],
    ids=["没有名字", "值里有空格", "全是分隔符", "只有名字"],
)
def test_is_authorized_treats_malformed_cookie_header_as_logged_out(header: str) -> None:
    """畸形 Cookie 按未登录处理：不能抛异常，也不能被当成有效会话。"""
    assert _guard().is_authorized({"cookie": header}) is False


# ---- Cookie 属性 ------------------------------------------------------------


def test_login_cookie_carries_security_attributes() -> None:
    cookie = _guard().login_cookie("token-value")

    assert cookie.startswith(f"{COOKIE_NAME}=token-value")
    assert "HttpOnly" in cookie
    # Lax 而非 Strict：写操作全是 POST，Lax 已经不在跨站请求上带 Cookie；
    # Strict 会让「点链接进来」的顶层跳转也不带，用户每次访问都要重新登录。
    assert "SameSite=Lax" in cookie
    assert f"Max-Age={SESSION_TTL_SECONDS}" in cookie
    # 明文 HTTP 部署下加 Secure 会让直连彻底登不进去，因此这里必须没有它。
    assert "Secure" not in cookie


def test_logout_cookie_expires_immediately() -> None:
    cookie = _guard().logout_cookie()

    assert cookie.startswith(f"{COOKIE_NAME}=;")
    assert "Max-Age=0" in cookie
    assert "SameSite=Lax" in cookie


# ---- 凭据比对 ---------------------------------------------------------------


def test_authenticate_accepts_exact_credentials() -> None:
    assert _guard().authenticate(USERNAME, PASSWORD) is True


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (USERNAME, "wrong"),
        ("wrong", PASSWORD),
        (USERNAME.upper(), PASSWORD),
        (USERNAME, PASSWORD + " "),
    ],
    ids=["密码错", "用户名错", "用户名大小写不符", "密码多一个空格"],
)
def test_authenticate_rejects_anything_else(username: str, password: str) -> None:
    assert _guard().authenticate(username, password) is False


@pytest.mark.parametrize(
    "value",
    [None, 123, ["admin"], {"u": "admin"}, b"admin"],
    ids=["None", "整数", "列表", "字典", "bytes"],
)
def test_authenticate_rejects_non_string_input_without_raising(value: object) -> None:
    """JSON 请求体里字段可以是任意类型，比对函数不能因此抛 TypeError 变成 500。"""
    assert _guard().authenticate(value, value) is False


# ---- 失败次数节流 -----------------------------------------------------------


class _FakeClock:
    """节流用单调时钟，测试里换成可手动推进的假时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock: _FakeClock, max_failures: int = 3) -> LoginThrottle:
    return LoginThrottle(max_failures=max_failures, window_seconds=900.0, clock=clock)


def test_throttle_allows_requests_below_the_limit() -> None:
    clock = _FakeClock()
    throttle = _throttle(clock)

    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")

    assert throttle.is_blocked("10.0.0.1") is False


def test_throttle_blocks_after_reaching_the_limit() -> None:
    clock = _FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_failure("10.0.0.1")

    assert throttle.is_blocked("10.0.0.1") is True


def test_throttle_window_expiry_restores_access() -> None:
    clock = _FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_failure("10.0.0.1")

    clock.advance(901.0)

    assert throttle.is_blocked("10.0.0.1") is False


def test_throttle_counts_clients_separately() -> None:
    clock = _FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_failure("10.0.0.1")

    assert throttle.is_blocked("10.0.0.2") is False


def test_throttle_reset_clears_failures() -> None:
    """登录成功即清零：合法用户不该被自己之前的输错连累。"""
    clock = _FakeClock()
    throttle = _throttle(clock)
    for _ in range(2):
        throttle.record_failure("10.0.0.1")

    throttle.reset("10.0.0.1")
    throttle.record_failure("10.0.0.1")

    assert throttle.is_blocked("10.0.0.1") is False


def test_throttle_forgets_clients_outside_the_window() -> None:
    """长期运行不能无限堆积来源地址：过期条目在每次写入时被清理。"""
    clock = _FakeClock()
    throttle = _throttle(clock)
    for index in range(200):
        throttle.record_failure(f"10.0.0.{index}")
    clock.advance(901.0)

    throttle.record_failure("10.0.9.9")

    assert throttle.tracked_clients == 1


def test_throttle_bounds_the_tracked_client_table() -> None:
    """只有「清理过期条目」不够：窗口内涌入大量不同来源时表仍会无限长。

    表长直接影响每次写入的扫描耗时，所以必须有硬上限。
    """
    clock = _FakeClock()
    throttle = _throttle(clock)
    for index in range(MAX_TRACKED_CLIENTS + 50):
        throttle.record_failure(f"10.0.{index // 256}.{index % 256}")
        clock.advance(0.001)

    assert throttle.tracked_clients <= MAX_TRACKED_CLIENTS


def test_throttle_reports_no_retry_after_when_not_blocked() -> None:
    clock = _FakeClock()
    throttle = _throttle(clock)

    assert throttle.retry_after_seconds("10.0.0.1") == 0


def test_throttle_retry_after_counts_down_towards_the_window() -> None:
    """429 要带 Retry-After，告诉调用方等多久，而不是让它盲目重试。"""
    clock = _FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_failure("10.0.0.1")

    assert throttle.retry_after_seconds("10.0.0.1") == 900

    clock.advance(600.0)

    assert throttle.retry_after_seconds("10.0.0.1") == 300
    assert throttle.is_blocked("10.0.0.1") is True
