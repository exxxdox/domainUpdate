"""新建线程默认栈大小的配置测试。

musl libc（Alpine 镜像）的默认线程栈是 1MiB，glibc 是 8MiB。CPython 解析深层
嵌套 JSON 时，_json 的 C 递归在 1MiB 栈上先撞到栈边界并触发 SIGSEGV，来不及抛
RecursionError：同一个 40KB 请求体，glibc 上记一条错误日志继续服务，musl 上整个
进程退出（exit 139）。这里锁定抹平该差异的配置。
"""

import json
import logging
import threading
from collections.abc import Iterator

import pytest

from domain_update.thread_stack import (
    DEFAULT_THREAD_STACK_BYTES,
    configure_thread_stack,
)

# 还原全局状态时不能走 threading.stack_size：本文件会把它替换成桩函数，
# 只有先捕获原始函数，才能保证无论测试怎么打桩都还原得回去。
_real_stack_size = threading.stack_size


@pytest.fixture(autouse=True)
def _restore_thread_stack() -> Iterator[None]:
    """栈大小是进程级全局状态，任何改动都必须在测试结束后还原。"""
    original = _real_stack_size()
    yield
    _real_stack_size(original)


def test_default_is_at_least_eight_mib() -> None:
    # 8MiB 与 glibc 默认一致；低于该值在 musl 上会段错误。
    assert DEFAULT_THREAD_STACK_BYTES >= 8 * 1024 * 1024


def test_applies_default_size() -> None:
    configure_thread_stack()

    assert _real_stack_size() == DEFAULT_THREAD_STACK_BYTES


def test_applies_explicit_size() -> None:
    configure_thread_stack(4 * 1024 * 1024)

    assert _real_stack_size() == 4 * 1024 * 1024


def test_falls_back_when_platform_rejects_size(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _reject(_size: int) -> None:
        raise ValueError("size not valid")

    monkeypatch.setattr(threading, "stack_size", _reject)
    with caplog.at_level(logging.WARNING):
        result = configure_thread_stack(1)

    # 0 表示沿用平台默认值。栈大小不该让服务起不来，但静默吞掉会让问题无从排查。
    assert result == 0
    assert "线程栈" in caplog.text


def test_deep_json_in_worker_thread_raises_recursion_error() -> None:
    """配置生效后，深嵌套 JSON 只抛 RecursionError，进程不被拖垮。

    这是 musl 上的回归护栏：未配置栈大小时该线程会直接段错误，测试进程随之中断。
    """
    configure_thread_stack()
    outcome: list[str] = []

    def worker() -> None:
        try:
            json.loads("[" * 50_000 + "]" * 50_000)
        except RecursionError:
            outcome.append("recursion")
        except Exception as error:  # noqa: BLE001 - 记录异常类型以便断言
            outcome.append(type(error).__name__)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert outcome == ["recursion"]
