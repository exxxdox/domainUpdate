"""供 Streamlit、定时任务和命令行共同调用的应用服务。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from domain_update.config import AppConfig, ConfigStore
from domain_update.history import CheckHistoryStore, CheckRecord
from domain_update.models import DnsStatus, Result, UpdateStatus
from domain_update.network import get_public_ipv6
from domain_update.notifier import send_gotify
from domain_update.providers import create_provider


_UPDATE_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


class DomainUpdateService:
    def __init__(
        self,
        config_store: ConfigStore | None = None,
        history_store: CheckHistoryStore | None = None,
    ) -> None:
        self.config_store = config_store or ConfigStore()
        # 历史与配置放同一数据目录，单目录挂载即可完成持久化；测试可注入替身。
        self.history_store = history_store or CheckHistoryStore(
            getattr(self.config_store, "data_dir", None)
        )

    def load_config(self) -> Result[AppConfig]:
        return self.config_store.load()

    def save_config(self, config: AppConfig) -> Result[AppConfig]:
        return self.config_store.save(config)

    def get_public_ipv6(self) -> Result[str]:
        return get_public_ipv6()

    def send_test_notification(self, config: AppConfig) -> Result[bool]:
        """发送一条测试通知，用于在保存前验证 Gotify 地址与 Token 是否可用。

        故意不落盘：调用方传入的是“已保存配置 + 表单当前值”的合并结果，
        用户可以先把新凭据测通再保存，避免把错误配置写进配置文件。
        """
        result = send_gotify(
            config, "测试通知", "IPv6 域名控制台测试消息，收到即表示通知配置可用。"
        )
        # send_gotify 在未配置时返回成功但 sent=False；对“测试”场景这算失败，
        # 用户点测试按钮就是要一条真实到达的消息。
        if not result.ok or not result.data:
            # 原消息是给“DNS 已更新”场景写的，这里换成测试场景的说明，原始原因留在日志。
            logger.warning("Gotify 测试消息发送失败：%s", result.message)
            return Result.failure("测试消息发送失败，请检查地址、Token 与网络连通性")
        return Result.success("测试消息已发送，请在 Gotify 客户端确认是否收到", True)

    def get_dns_status(self) -> Result[DnsStatus]:
        config_result = self.load_config()
        if not config_result.ok or config_result.data is None:
            return Result.failure(config_result.message)
        config = config_result.data
        validation = config.validate()
        if not validation.ok:
            return Result.failure(validation.message)
        status = create_provider(config).get_status()
        if not status.ok:
            return Result.failure(status.message)
        if status.data is None:
            # 页面需要区分“尚未创建”与“查询失败”，同时展示目标记录名称。
            empty_status = DnsStatus(
                provider="cloudflare",
                record_name=config.cloudfare_record_name,
                record_type="AAAA",
                value="",
                record_id="",
            )
            return Result.success(status.message, empty_status)
        return Result.success(status.message, status.data)

    def check_and_update(self, source: str = "manual") -> Result[UpdateStatus]:
        # 防止页面手动操作与后台定时检查同时写同一条 DNS 记录。
        # 页面、定时器和命令行实例共享同一把锁，避免并发写同一条记录。
        with _UPDATE_LOCK:
            result = self._perform_update()
            # 成功与失败都记入历史，报告才能反映真实成功率。
            self._record_check(source, result)
            logger.info(
                "检查完成：来源=%s 成功=%s 动作=%s 消息=%s",
                source,
                result.ok,
                result.data.action if result.data is not None else "failed",
                result.message,
            )
            return result

    def _perform_update(self) -> Result[UpdateStatus]:
        config_result = self.load_config()
        if not config_result.ok or config_result.data is None:
            return Result.failure(config_result.message)
        config = config_result.data
        validation = config.validate()
        if not validation.ok:
            return Result.failure(validation.message)

        ipv6_result = get_public_ipv6()
        if not ipv6_result.ok or ipv6_result.data is None:
            return Result.failure(ipv6_result.message)
        ipv6 = ipv6_result.data
        provider = create_provider(config)
        dns_result = provider.get_status()
        if not dns_result.ok:
            # 查询失败时不得把“状态未知”误判为“不存在”并尝试创建重复记录。
            return Result.failure(dns_result.message)
        current = dns_result.data
        # Cloudflare 允许记录缺失时创建；阿里云依赖 Record ID，适配器会明确拒绝。
        write_result = provider.set_ipv6(ipv6, current)
        if not write_result.ok or write_result.data is None:
            return Result.failure(write_result.message)

        action = write_result.data
        previous = current.value if current is not None else None
        status = UpdateStatus(
            provider=config.provider,
            action=action,
            ipv6=ipv6,
            previous_value=previous,
            current_value=ipv6,
            # 记录名以查询结果为准：两家里只有 Cloudflare 把它写进配置，
            # 这里统一带上，页面就不必按服务商分叉推断。
            record_name=current.record_name if current is not None else "",
        )
        message = write_result.message
        if action in {"created", "updated"}:
            notification = send_gotify(
                config,
                "DNS IPv6 已更新",
                f"{config.provider}：{previous or '无记录'} -> {ipv6}",
            )
            if not notification.ok:
                message = f"{message}；{notification.message}"
        return Result.success(message, status)

    def _record_check(self, source: str, result: Result[UpdateStatus]) -> None:
        """历史是旁路能力：写不进去也不能影响 DNS 更新的返回结果。"""
        status = result.data
        written = self.history_store.append(
            CheckRecord(
                timestamp=datetime.now().astimezone(),
                source=source,
                ok=result.ok,
                action=status.action if status is not None else "failed",
                message=result.message,
                ipv6=status.ipv6 if status is not None else "",
                previous_value=status.previous_value if status is not None else None,
            )
        )
        if not written.ok:
            # 历史写入现在是完全静默的，报告缺记录时无从判断是没执行还是没写进去。
            logger.warning("检查记录写入失败：%s", written.message)


def create_service() -> DomainUpdateService:
    return DomainUpdateService()

