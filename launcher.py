"""先恢复后台定时任务，再启动控制台 HTTP 服务。"""

from __future__ import annotations

import logging

from domain_update.auth import (
    WEB_PASSWORD_ENV,
    WEB_USERNAME_ENV,
    AuthGuard,
    missing_credential_variables,
)
from domain_update.config import ConfigStore
from domain_update.logging_setup import configure_logging
from domain_update.scheduler import get_scheduler
from domain_update.thread_stack import configure_thread_stack
from domain_update.web.server import resolve_web_port, serve


HOST = "0.0.0.0"

logger = logging.getLogger(__name__)


def main() -> int:
    # 日志必须最先配置：连配置读取失败这类启动期问题也要能在 docker logs 里看到。
    configure_logging()
    # 必须早于任何线程创建：下面的调度器线程和控制台的请求线程都在此之后才出现，
    # 而 stack_size 只对调用之后新建的线程生效。
    configure_thread_stack()

    store = ConfigStore()
    # 端口先解析再打印：日志里的监听地址必须和真实端口一致，
    # 否则 WEB_PORT 写错时，日志会指向一个根本没在监听的端口。
    port = resolve_web_port()
    # 恰好只配了一个变量（多半是名字写错）不能按「未配置」放行：那会让人以为开了登录，
    # 实际控制台裸奔。这里直接拒绝启动，把问题摁在能看到日志的启动阶段。
    # 两个都不配仍然是允许的部署方式（下面 AuthGuard.from_env() 会记一条 WARNING）。
    missing = missing_credential_variables()
    if len(missing) == 1:
        logger.error(
            "登录凭据只配置了一半（缺少 %s），拒绝启动：请同时设置 %s 与 %s，"
            "或两个都留空以明确不启用登录",
            missing[0],
            WEB_USERNAME_ENV,
            WEB_PASSWORD_ENV,
        )
        return 2

    # 守卫在这里构造，而不是留给 serve() 的默认值：load_credentials 可能打一条
    # 「未配置凭据」的 WARNING，必须晚于 configure_logging()，否则最该被看到的那条日志会丢。
    auth = AuthGuard.from_env()
    logger.info(
        "启动控制台：数据目录=%s 监听=%s:%s 登录=%s",
        store.data_dir,
        HOST,
        port,
        "已启用" if auth.enabled else "未启用",
    )

    config_result = store.load()
    if config_result.ok and config_result.data is not None:
        # 容器启动即恢复调度，不依赖用户先打开网页触发。
        scheduler_result = get_scheduler().configure(config_result.data)
        logger.info("定时检查状态：%s", scheduler_result.message)
    else:
        # 不阻断启动：允许用户打开页面完成首次配置。
        logger.warning("未能恢复定时检查：%s", config_result.message)

    # 必须把上面构造好的守卫传进去：不传的话 serve() 会再读一次环境变量，
    # 结果是启动日志出现两条「未配置凭据」的 WARNING，而且真正生效的是第二个实例。
    return serve(host=HOST, port=port, auth=auth)


if __name__ == "__main__":
    raise SystemExit(main())
