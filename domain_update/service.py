"""供 Streamlit、定时任务和命令行共同调用的应用服务。"""

from __future__ import annotations

import threading

from domain_update.config import AppConfig, ConfigStore
from domain_update.models import DnsStatus, Result, UpdateStatus
from domain_update.network import get_public_ipv6
from domain_update.notifier import send_gotify
from domain_update.providers import create_provider


_UPDATE_LOCK = threading.Lock()


class DomainUpdateService:
    def __init__(self, config_store: ConfigStore | None = None) -> None:
        self.config_store = config_store or ConfigStore()

    def load_config(self) -> Result[AppConfig]:
        return self.config_store.load()

    def save_config(self, config: AppConfig) -> Result[AppConfig]:
        return self.config_store.save(config)

    def get_public_ipv6(self) -> Result[str]:
        return get_public_ipv6()

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

    def check_and_update(self) -> Result[UpdateStatus]:
        # 防止页面手动操作与后台定时检查同时写同一条 DNS 记录。
        # 页面、定时器和命令行实例共享同一把锁，避免并发写同一条记录。
        with _UPDATE_LOCK:
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


def create_service() -> DomainUpdateService:
    return DomainUpdateService()

