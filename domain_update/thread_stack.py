"""新建线程的默认栈大小配置。

musl libc（Alpine 镜像）的默认线程栈是 1MiB，glibc 是 8MiB。CPython 解析深层
嵌套 JSON 时，_json 的 C 递归在 1MiB 栈上先撞到栈边界并触发 SIGSEGV，来不及抛
RecursionError：实测同一个 40KB 请求体，glibc 上记一条错误日志继续服务，musl 上
整个进程退出（exit 139）。

在启动期把默认栈抬到与 glibc 一致，是为了让“换基础镜像”不改变进程的安全边界 ——
否则同一份代码在两种 libc 下的健壮性不同，而这种差异只会在出事时才暴露。
"""

from __future__ import annotations

import logging
import threading

# 与 glibc 默认线程栈一致。实测该尺寸下 50000 层嵌套 JSON 抛 RecursionError 而非段错误。
DEFAULT_THREAD_STACK_BYTES = 8 * 1024 * 1024

logger = logging.getLogger(__name__)


def configure_thread_stack(size: int = DEFAULT_THREAD_STACK_BYTES) -> int:
    """设置新建线程的默认栈大小，返回实际生效的值。

    只能影响调用之后创建的线程，因此必须早于任何线程。

    返回 0 表示平台拒绝了该尺寸、沿用系统默认值。栈大小只决定可用的递归深度，
    不该因为它让服务起不来，所以失败只记警告。
    """
    try:
        threading.stack_size(size)
    except (ValueError, RuntimeError) as error:
        logger.warning("线程栈大小 %s 字节设置失败：%s，沿用系统默认值", size, error)
        return 0
    return size
