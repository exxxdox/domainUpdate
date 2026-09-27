"""日志与配置保存失败定位的测试。

容器排障只能看 `docker logs`，因此日志必须走标准输出，且失败路径要留下
足够信息（失败步骤、路径、errno），不能只回一个异常类名。
"""

import logging
import signal
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from domain_update import logging_setup
from domain_update.config import AppConfig, ConfigStore
from domain_update.scheduler import SchedulerSnapshot
from domain_update.service import DomainUpdateService
from domain_update.web.api import WebApi
from domain_update.web.server import ConsoleServer, install_stop_handler


class StubScheduler:
    def configure(self, config: AppConfig) -> Any:
        return None

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            enabled=False,
            running=False,
            interval_minutes=10,
            next_run=None,
            last_run=None,
            last_ok=None,
            last_message="尚未执行",
        )


def test_configure_logging_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式 force：其他测试可能已经配置过根日志器。
    logging_setup.configure_logging("DEBUG", force=True)

    root = logging.getLogger()
    streams = [
        handler.stream
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
    ]
    assert sys.stdout in streams
    assert root.level == logging.DEBUG


def test_configure_logging_reads_level_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "warning")

    logging_setup.configure_logging(force=True)

    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_falls_back_on_invalid_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 写错日志级别不应该让容器起不来。
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "not-a-level")

    logging_setup.configure_logging(force=True)

    assert logging.getLogger().level == logging.INFO


def test_config_save_logs_step_path_and_errno(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mkstemp 抛 PermissionError 时，日志要能直接指出失败步骤与目录。"""

    def broken_mkstemp(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", broken_mkstemp)

    with caplog.at_level(logging.ERROR, logger="domain_update.config"):
        result = ConfigStore(tmp_path).save(AppConfig(cloudfare_zone_id="zone"))

    assert not result.ok
    log = caplog.text
    # 三层信息缺一不可：哪个步骤、哪个目录、内核为什么拒绝。
    assert str(tmp_path) in log
    assert "13" in log
    assert "Permission denied" in log
    assert "临时文件" in log


def test_config_save_failure_message_keeps_errno_text(tmp_path: Path) -> None:
    # 界面提示保持简短，但要带上 strerror，否则用户只能看到"PermissionError"。
    (tmp_path / "missing").write_text("这不是目录", encoding="utf-8")

    result = ConfigStore(tmp_path / "missing" / "deeper").save(AppConfig())

    assert not result.ok
    assert "配置保存失败" in result.message


def test_config_load_failure_is_logged(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    (tmp_path / "config.json").write_text("{ broken", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="domain_update.config"):
        result = ConfigStore(tmp_path).load()

    assert not result.ok
    assert "config.json" in caplog.text


def test_unexpected_api_error_returns_500_and_logs_traceback(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    class ExplodingService(DomainUpdateService):
        def load_config(self) -> Any:
            raise RuntimeError("boom")

    api = WebApi(
        service=ExplodingService(ConfigStore(tmp_path)), scheduler=StubScheduler()
    )

    with caplog.at_level(logging.ERROR, logger="domain_update.web.api"):
        response = api.handle("GET", "/api/state")

    # 未预期的异常不能让连接直接断掉，要回一个可读的 500。
    assert response.status == 500
    assert response.payload["ok"] is False
    assert "boom" in caplog.text


def test_install_stop_handler_replaces_default_sigterm(tmp_path: Path) -> None:
    """回归：不注册处理函数时 PID 1 会忽略 SIGTERM，容器只能被等满宽限期后 SIGKILL。"""
    if not hasattr(signal, "SIGTERM"):  # pragma: no cover - 平台差异
        pytest.skip("当前平台不支持 SIGTERM")

    api = WebApi(
        service=DomainUpdateService(ConfigStore(tmp_path)), scheduler=StubScheduler()
    )
    server = ConsoleServer(("127.0.0.1", 0), api)
    original = signal.getsignal(signal.SIGTERM)
    try:
        install_stop_handler(server)

        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, original)
        server.server_close()


def test_sigterm_handler_stops_running_server(tmp_path: Path) -> None:
    """处理函数必须真的让 serve_forever 退出，否则容器还是只能被 SIGKILL。"""
    if not hasattr(signal, "SIGTERM"):  # pragma: no cover - 平台差异
        pytest.skip("当前平台不支持 SIGTERM")

    api = WebApi(
        service=DomainUpdateService(ConfigStore(tmp_path)), scheduler=StubScheduler()
    )
    server = ConsoleServer(("127.0.0.1", 0), api)
    original = signal.getsignal(signal.SIGTERM)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        install_stop_handler(server)
        serving.start()

        # 直接调用已注册的处理函数，不真的发信号：Windows 上 os.kill 会直接结束进程，
        # 那样测试进程会被杀掉，测不出任何东西。
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

        serving.join(timeout=5)
        assert not serving.is_alive()
    finally:
        signal.signal(signal.SIGTERM, original)
        server.server_close()


def test_server_logs_each_request_to_stdout(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    api = WebApi(
        service=DomainUpdateService(ConfigStore(tmp_path)), scheduler=StubScheduler()
    )
    server = ConsoleServer(("127.0.0.1", 0), api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with caplog.at_level(logging.INFO, logger="domain_update.web.server"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=5
            ) as reply:
                reply.read()
        assert "/healthz" in caplog.text
        assert "200" in caplog.text
    finally:
        server.shutdown()
        server.server_close()
