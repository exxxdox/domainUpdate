"""先恢复后台定时任务，再启动控制台 HTTP 服务。"""

from __future__ import annotations

import logging

from domain_update.config import ConfigStore
from domain_update.logging_setup import configure_logging
from domain_update.scheduler import get_scheduler
from domain_update.web.server import serve


HOST = "0.0.0.0"
# 容器内必须监听所有网卡端口映射才生效；对外暴露面由 compose 的端口绑定控制。
PORT = 8501

logger = logging.getLogger(__name__)


def main() -> int:
    # 日志必须最先配置：连配置读取失败这类启动期问题也要能在 docker logs 里看到。
    configure_logging()

    store = ConfigStore()
    logger.info("启动控制台：数据目录=%s 监听=%s:%s", store.data_dir, HOST, PORT)

    config_result = store.load()
    if config_result.ok and config_result.data is not None:
        # 容器启动即恢复调度，不依赖用户先打开网页触发。
        scheduler_result = get_scheduler().configure(config_result.data)
        logger.info("定时检查状态：%s", scheduler_result.message)
    else:
        # 不阻断启动：允许用户打开页面完成首次配置。
        logger.warning("未能恢复定时检查：%s", config_result.message)

    return serve(host=HOST, port=PORT)


if __name__ == "__main__":
    raise SystemExit(main())
