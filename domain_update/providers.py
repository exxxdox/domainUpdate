"""DNS 服务商适配层。"""

from __future__ import annotations

from typing import Protocol

from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from cloudflare import Cloudflare

from domain_update.config import AppConfig
from domain_update.models import DnsStatus, Result, UpdateAction


class DnsProvider(Protocol):
    name: str

    def get_status(self) -> Result[DnsStatus | None]: ...

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]: ...


class CloudflareProvider:
    name = "cloudflare"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = Cloudflare(api_token=config.cloudfare_token)

    def get_status(self) -> Result[DnsStatus | None]:
        try:
            records = self._client.dns.records.list(
                zone_id=self._config.cloudfare_zone_id,
                name=self._config.cloudfare_record_name,
                type="AAAA",
            )
            for record in records:
                if (
                    record.name == self._config.cloudfare_record_name
                    and record.type == "AAAA"
                ):
                    status = DnsStatus(
                        provider="cloudflare",
                        record_name=record.name,
                        record_type="AAAA",
                        value=record.content,
                        record_id=record.id,
                        proxied=getattr(record, "proxied", None),
                        ttl=getattr(record, "ttl", None),
                    )
                    return Result.success("Cloudflare DNS 记录读取成功", status)
            # “记录不存在”不是请求失败，更新流程可据此安全地创建记录。
            return Result.success("Cloudflare 中未找到对应的 AAAA 记录", None)
        except Exception:
            # SDK 异常可能包含请求头或服务端细节，不向界面透传。
            return Result.failure("Cloudflare DNS 查询失败，请检查凭据与 Zone ID")

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]:
        try:
            if current is None:
                self._client.dns.records.create(
                    zone_id=self._config.cloudfare_zone_id,
                    content=ipv6,
                    name=self._config.cloudfare_record_name,
                    proxied=False,
                    type="AAAA",
                    ttl=60,
                )
                return Result.success("Cloudflare AAAA 记录创建成功", "created")
            if current.value == ipv6:
                return Result.success("Cloudflare AAAA 记录无需更新", "unchanged")
            self._client.dns.records.edit(
                dns_record_id=current.record_id,
                zone_id=self._config.cloudfare_zone_id,
                content=ipv6,
                # 必须使用配置中的完整记录名，避免旧实现硬编码 server 后改错记录。
                name=self._config.cloudfare_record_name,
                type="AAAA",
                # 保留原记录的代理与 TTL，IPv6 变化不应改变暴露方式和缓存策略。
                **(
                    {"proxied": current.proxied}
                    if current.proxied is not None
                    else {}
                ),
                **({"ttl": current.ttl} if current.ttl is not None else {}),
            )
            return Result.success("Cloudflare AAAA 记录更新成功", "updated")
        except Exception:
            return Result.failure("Cloudflare DNS 写入失败，请检查凭据与记录配置")


class AlibabaProvider:
    name = "alibaba"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        client_config = open_api_models.Config(
            access_key_id=config.alibaba_cloud_access_key_id,
            access_key_secret=config.alibaba_cloud_access_key_secret,
        )
        client_config.endpoint = "alidns.cn-hangzhou.aliyuncs.com"
        self._client = AlidnsClient(client_config)

    def get_status(self) -> Result[DnsStatus | None]:
        request = alidns_models.DescribeDomainRecordInfoRequest(
            record_id=self._config.alibaba_cloud_record_id
        )
        try:
            response = self._client.describe_domain_record_info_with_options(
                request, util_models.RuntimeOptions()
            )
            body = response.body
            value = body.value or ""
            rr = body.rr or self._config.alibaba_cloud_rr
            record_type = body.type or self._config.alibaba_cloud_ip_type
            status = DnsStatus(
                provider="alibaba",
                record_name=rr,
                record_type=record_type,
                value=value,
                record_id=self._config.alibaba_cloud_record_id,
            )
            return Result.success("阿里云 DNS 记录读取成功", status)
        except Exception:
            return Result.failure("阿里云 DNS 查询失败，请检查凭据与 Record ID")

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]:
        if current is None:
            # 阿里云配置以 Record ID 为主键，缺失记录无法安全推导域名并新建。
            return Result.failure("阿里云记录不存在或不可访问，无法按 Record ID 更新")
        if current.value == ipv6:
            return Result.success("阿里云 AAAA 记录无需更新", "unchanged")
        request = alidns_models.UpdateDomainRecordRequest(
            record_id=self._config.alibaba_cloud_record_id,
            # Record ID 查询结果是事实来源，避免错误配置把已有记录重命名。
            rr=current.record_name,
            type=current.record_type.upper() or "AAAA",
            value=ipv6,
        )
        try:
            self._client.update_domain_record_with_options(
                request, util_models.RuntimeOptions()
            )
            return Result.success("阿里云 AAAA 记录更新成功", "updated")
        except Exception:
            return Result.failure("阿里云 DNS 写入失败，请检查凭据与记录配置")


def create_provider(config: AppConfig) -> DnsProvider:
    if config.provider == "cloudflare":
        return CloudflareProvider(config)
    return AlibabaProvider(config)

