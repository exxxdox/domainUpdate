"""服务商适配层测试。

Cloudflare 适配器已从官方 SDK 改为直接调用 REST API（去掉 SDK 可省约 62MB 镜像体积），
因此这里改为注入可记录的假 Session，断言真实发出的 HTTP 请求，而不是 SDK 的方法调用。
"""

from typing import Any
from unittest.mock import MagicMock, patch

import requests

from domain_update.config import AppConfig
from domain_update.models import DnsStatus
from domain_update.providers import (
    CLOUDFLARE_API_BASE,
    AlibabaProvider,
    CloudflareProvider,
)


def cloudflare_config(token: str = "token") -> AppConfig:
    return AppConfig(
        cloudfare_token=token,
        cloudfare_zone_id="zone-id",
        cloudfare_record_name="home.example.com",
    )


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class RecordingSession:
    """记录每次请求的假 Session，充当 requests.Session 的最小替身。"""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse({"success": True, "result": []})

    @property
    def last_call(self) -> dict[str, Any]:
        assert self.calls, "适配器没有发出任何请求"
        return self.calls[-1]


def cloudflare_provider(
    session: RecordingSession, config: AppConfig | None = None
) -> CloudflareProvider:
    return CloudflareProvider(config or cloudflare_config(), session=session)


def test_cloudflare_sends_bearer_token_from_config() -> None:
    session = RecordingSession()

    cloudflare_provider(session)

    assert session.headers["Authorization"] == "Bearer token"


def test_cloudflare_instances_do_not_share_credentials() -> None:
    # 凭据写在实例级 Session 上，两个适配器不能互相串用请求头。
    first = CloudflareProvider(cloudflare_config("token-a"))
    second = CloudflareProvider(cloudflare_config("token-b"))

    assert first._session.headers["Authorization"] == "Bearer token-a"
    assert second._session.headers["Authorization"] == "Bearer token-b"


def test_cloudflare_lists_records_with_type_and_name_filter() -> None:
    session = RecordingSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "result": [
                        {
                            "id": "record-id",
                            "name": "home.example.com",
                            "type": "AAAA",
                            "content": "240e::1",
                            "proxied": True,
                            "ttl": 300,
                        }
                    ],
                }
            )
        ]
    )

    result = cloudflare_provider(session).get_status()

    assert result.ok
    assert result.data is not None
    assert result.data.value == "240e::1"
    assert result.data.record_id == "record-id"
    assert result.data.proxied is True
    assert result.data.ttl == 300
    call = session.last_call
    assert call["method"] == "GET"
    assert call["url"] == f"{CLOUDFLARE_API_BASE}/zones/zone-id/dns_records"
    assert call["params"] == {"type": "AAAA", "name": "home.example.com"}


def test_cloudflare_ignores_records_with_other_type() -> None:
    # 记录类型过滤不能只依赖服务端：返回内容里混入 A 记录时不能被当成 AAAA 记录。
    session = RecordingSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "result": [
                        {
                            "id": "other-id",
                            "name": "home.example.com",
                            "type": "A",
                            "content": "1.2.3.4",
                        }
                    ],
                }
            )
        ]
    )

    result = cloudflare_provider(session).get_status()

    assert result.ok
    assert result.data is None


def test_cloudflare_reports_missing_record_without_failure() -> None:
    session = RecordingSession([FakeResponse({"success": True, "result": []})])

    result = cloudflare_provider(session).get_status()

    assert result.ok
    assert result.data is None


def test_cloudflare_query_failure_returns_stable_message() -> None:
    session = RecordingSession([FakeResponse({"success": False, "errors": []}, 403)])

    result = cloudflare_provider(session).get_status()

    assert not result.ok
    # 失败消息不得包含响应体或凭据细节，界面只展示固定文案。
    assert result.message == "Cloudflare DNS 查询失败，请检查凭据与 Zone ID"
    assert "token" not in result.message


def test_cloudflare_treats_success_false_as_failure() -> None:
    # Cloudflare 会在 HTTP 200 的同时返回 success=false，只判断状态码会漏掉这类失败。
    session = RecordingSession([FakeResponse({"success": False, "errors": []})])

    result = cloudflare_provider(session).get_status()

    assert not result.ok


def test_cloudflare_update_uses_configured_record_name() -> None:
    session = RecordingSession([FakeResponse({"success": True, "result": {}})])
    provider = cloudflare_provider(session)
    current = DnsStatus(
        provider="cloudflare",
        record_name="home.example.com",
        record_type="AAAA",
        value="240e::1",
        record_id="record-id",
        proxied=True,
        ttl=300,
    )

    result = provider.set_ipv6("240e::2", current)

    assert result.ok
    assert result.data == "updated"
    call = session.last_call
    assert call["method"] == "PATCH"
    assert call["url"] == f"{CLOUDFLARE_API_BASE}/zones/zone-id/dns_records/record-id"
    assert call["json"] == {
        "content": "240e::2",
        # 必须使用配置中的完整记录名，避免旧实现硬编码 server 后改错记录。
        "name": "home.example.com",
        "type": "AAAA",
        # 保留原记录的代理与 TTL，IPv6 变化不应改变暴露方式和缓存策略。
        "proxied": True,
        "ttl": 300,
    }


def test_cloudflare_update_omits_unknown_proxied_and_ttl() -> None:
    session = RecordingSession([FakeResponse({"success": True, "result": {}})])
    provider = cloudflare_provider(session)
    current = DnsStatus(
        provider="cloudflare",
        record_name="home.example.com",
        record_type="AAAA",
        value="240e::1",
        record_id="record-id",
    )

    provider.set_ipv6("240e::2", current)

    assert "proxied" not in session.last_call["json"]
    assert "ttl" not in session.last_call["json"]


def test_cloudflare_creates_record_when_missing() -> None:
    session = RecordingSession([FakeResponse({"success": True, "result": {}})])

    result = cloudflare_provider(session).set_ipv6("240e::2", None)

    assert result.ok
    assert result.data == "created"
    call = session.last_call
    assert call["method"] == "POST"
    assert call["url"] == f"{CLOUDFLARE_API_BASE}/zones/zone-id/dns_records"
    assert call["json"] == {
        "content": "240e::2",
        "name": "home.example.com",
        "type": "AAAA",
        "proxied": False,
        "ttl": 60,
    }


def test_cloudflare_skips_request_when_value_unchanged() -> None:
    session = RecordingSession()
    current = DnsStatus(
        provider="cloudflare",
        record_name="home.example.com",
        record_type="AAAA",
        value="240e::2",
        record_id="record-id",
    )

    result = cloudflare_provider(session).set_ipv6("240e::2", current)

    assert result.ok
    assert result.data == "unchanged"
    assert session.calls == []


def test_cloudflare_write_failure_returns_stable_message() -> None:
    session = RecordingSession([FakeResponse({"success": False}, 500)])

    result = cloudflare_provider(session).set_ipv6("240e::2", None)

    assert not result.ok
    assert result.message == "Cloudflare DNS 写入失败，请检查凭据与记录配置"


@patch("domain_update.providers.AlidnsClient")
def test_alibaba_update_uses_queried_record_identity(client_type: MagicMock) -> None:
    config = AppConfig(
        provider="alibaba",
        alibaba_cloud_access_key_id="key-id",
        alibaba_cloud_access_key_secret="secret",
        alibaba_cloud_record_id="record-id",
        alibaba_cloud_rr="wrong-config-value",
    )
    provider = AlibabaProvider(config)
    current = DnsStatus(
        provider="alibaba",
        record_name="actual-rr",
        record_type="AAAA",
        value="240e::1",
        record_id="record-id",
    )

    result = provider.set_ipv6("240e::2", current)

    assert result.ok
    request = client_type.return_value.update_domain_record_with_options.call_args.args[0]
    assert request.rr == "actual-rr"
    assert request.type == "AAAA"
