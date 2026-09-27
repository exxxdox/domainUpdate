"""Gotify 通知适配测试。

重点是凭据不外泄：Token 一旦出现在 URL 里，就会顺着 requests 的异常文本
（"Max retries exceeded with url: /message?token=..."）写进容器日志。
"""

import logging
from typing import Any

import pytest
import requests

from domain_update.config import AppConfig
from domain_update.notifier import send_gotify


def _response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://notify.example.com/message"
    return response


def test_sends_token_in_header_not_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _response(200)

    monkeypatch.setattr(requests, "post", fake_post)
    config = AppConfig(gotify_address="notify.example.com", gotify_token="secret-token")

    result = send_gotify(config, "标题", "内容")

    assert result.ok is True
    assert result.data is True
    # Token 走请求头：查询参数会进入异常文本、代理日志与访问日志。
    assert captured["headers"]["X-Gotify-Key"] == "secret-token"
    assert "secret-token" not in captured["url"]
    assert captured["url"] == "https://notify.example.com/message"
    assert captured["data"]["title"] == "标题"


def test_skips_request_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*args, **kwargs):  # pragma: no cover - 只用于断言未被调用
        raise AssertionError("未配置 Gotify 时不应发起请求")

    monkeypatch.setattr(requests, "post", fail_post)

    result = send_gotify(AppConfig(), "标题", "内容")

    assert result.ok is True
    assert result.data is False


def test_failure_log_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_post(url, **kwargs):
        # 还原 requests 的异常文本：里面带着完整请求 URL。
        raise requests.ConnectionError(
            "Max retries exceeded with url: /message?token=secret-token"
        )

    monkeypatch.setattr(requests, "post", fail_post)
    config = AppConfig(gotify_address="notify.example.com", gotify_token="secret-token")

    with caplog.at_level(logging.WARNING):
        result = send_gotify(config, "标题", "内容")

    assert result.ok is False
    assert "secret-token" not in caplog.text
    # 仍然要留下可排查的信息：异常类型。
    assert "ConnectionError" in caplog.text


def test_failure_log_redacts_token_from_url_bearing_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """回归防线：即使将来有人把 Token 挪回查询参数，日志里也必须被抹掉。"""

    def fail_post(url, **kwargs):
        raise requests.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://notify.example.com/message?token=other-secret&x=1"
        )

    monkeypatch.setattr(requests, "post", fail_post)
    config = AppConfig(gotify_address="notify.example.com", gotify_token="other-secret")

    with caplog.at_level(logging.WARNING):
        send_gotify(config, "标题", "内容")

    assert "other-secret" not in caplog.text
    assert "401" in caplog.text
