"""WEB_PORT 解析测试。

端口来自环境变量，是启动期就要定下来的值：
解析失败必须退回默认端口并留下日志，而不是让容器起不来。
"""

import logging

import pytest

from domain_update.web.server import DEFAULT_PORT, WEB_PORT_ENV, resolve_web_port


def test_defaults_when_unset() -> None:
    assert resolve_web_port({}) == DEFAULT_PORT


def test_reads_numeric_value() -> None:
    assert resolve_web_port({WEB_PORT_ENV: "9000"}) == 9000


def test_tolerates_surrounding_whitespace() -> None:
    assert resolve_web_port({WEB_PORT_ENV: " 9000 "}) == 9000


def test_falls_back_on_non_numeric(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert resolve_web_port({WEB_PORT_ENV: "http"}) == DEFAULT_PORT

    # 静默退回会让用户以为端口生效了，必须留下原因。
    assert WEB_PORT_ENV in caplog.text


@pytest.mark.parametrize("value", ["0", "-1", "65536", "99999"])
def test_falls_back_on_out_of_range(value: str) -> None:
    assert resolve_web_port({WEB_PORT_ENV: value}) == DEFAULT_PORT
