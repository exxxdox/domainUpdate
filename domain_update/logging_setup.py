"""统一日志配置。

容器排障只能看 `docker logs`，因此所有日志都写到标准输出，由容器运行时收集。
不显式配置的话，Python 只对 WARNING 以上使用无时间、无模块名的兜底格式，
出错时无法定位是哪个环节、什么时候发生的。
"""

from __future__ import annotations

import logging
import os
import sys


LOG_LEVEL_ENV = "DOMAIN_UPDATE_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 进程内只配置一次：重复配置会叠加 handler，同一条日志被打印多次。
_configured = False


def configure_logging(level: str | None = None, force: bool = False) -> None:
    """把根日志器接到标准输出。重复调用无副作用，除非显式 force。"""
    global _configured
    if _configured and not force:
        return

    resolved = (level or os.environ.get(LOG_LEVEL_ENV, DEFAULT_LEVEL)).upper()
    # 日志级别写错不应该让容器起不来，退回 INFO 即可。
    numeric_level = getattr(logging, resolved, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
        resolved = DEFAULT_LEVEL

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # http.server 的默认访问日志走 stderr 且格式与业务日志不一致，
    # 已在 server.py 里改成通过 logging 输出，这里只压掉它自己的兜底日志。
    logging.getLogger("http.server").setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).info("日志已初始化：输出=stdout 级别=%s", resolved)
